"""In-memory time-series store with an append-only file on disk."""

import bisect
import datetime
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

ALLOWED_TAG_KEYS: Set[str] = {"client", "site", "env", "service"}


class ValidationError(ValueError):
    """Raised when metric point or query parameters fail validation."""


def parse_ts(s: str, field: str = "ts") -> int:
    """Parse a UTC ISO 8601 timestamp string into epoch seconds."""
    if not isinstance(s, str) or not _TS_RE.match(s):
        raise ValidationError(f"{field}: must match YYYY-MM-DDTHH:MM:SSZ format")
    try:
        dt = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError as e:
        raise ValidationError(f"{field}: invalid date/time: {e}") from e
    return int(dt.timestamp())


def format_ts(epoch: int) -> str:
    """Format epoch seconds into a UTC ISO 8601 timestamp string."""
    dt = datetime.datetime.fromtimestamp(int(epoch), tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_point(raw: Any) -> Dict[str, Any]:
    """Validate and normalize a raw data point dictionary."""
    if not isinstance(raw, dict):
        raise ValidationError("point: must be a dict")

    if "name" not in raw:
        raise ValidationError("name: is required")
    name = raw["name"]
    if not isinstance(name, str):
        raise ValidationError("name: must be a string")
    if not _NAME_RE.match(name):
        raise ValidationError("name: must match ^[a-z][a-z0-9_]*$")

    if "value" not in raw:
        raise ValidationError("value: is required")
    val = raw["value"]
    if isinstance(val, bool):
        raise ValidationError("value: boolean is not allowed")
    if not isinstance(val, (int, float)):
        raise ValidationError("value: must be a number")
    if math.isnan(val) or math.isinf(val):
        raise ValidationError("value: cannot be NaN or infinity")
    value = float(val)

    if "ts" not in raw:
        raise ValidationError("ts: is required")
    ts_str = raw["ts"]
    parse_ts(ts_str, field="ts")

    tags_raw = raw.get("tags")
    if tags_raw is None:
        tags: Dict[str, str] = {}
    elif not isinstance(tags_raw, dict):
        raise ValidationError("tags: must be a dict")
    else:
        tags = {}
        for k, v in tags_raw.items():
            if not isinstance(k, str) or k not in ALLOWED_TAG_KEYS:
                raise ValidationError(f"tags: unknown tag key '{k}'")
            if not isinstance(v, str) or len(v) == 0:
                raise ValidationError(f"tags: value for '{k}' must be a non-empty string")
            if v != v.lower():
                raise ValidationError(f"tags: value for '{k}' must be lowercase")
            tags[k] = v

    return {
        "name": name,
        "value": value,
        "ts": ts_str,
        "tags": dict(sorted(tags.items())),
    }


class _Series:
    """Internal parallel storage for a single time series."""

    def __init__(self, name: str, tags: Dict[str, str]) -> None:
        self.name: str = name
        self.tags: Dict[str, str] = tags
        self.timestamps: List[int] = []
        self.values: List[float] = []

    def insert(self, epoch: int, value: float) -> None:
        idx = bisect.bisect_right(self.timestamps, epoch)
        self.timestamps.insert(idx, epoch)
        self.values.insert(idx, value)


class TimeSeriesStore:
    """In-memory time-series store with append-only persistence on disk."""

    def __init__(self, path: Optional[str] = None) -> None:
        """Initialize the store, optionally replaying points from disk."""
        self.path: Optional[str] = str(path) if path is not None else None
        self.skipped_lines: int = 0
        self._total_points: int = 0
        self._series_map: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], _Series] = {}
        self._by_metric: Dict[str, List[_Series]] = {}

        if self.path is not None:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.exists(self.path):
                self._replay()

    def _insert_point(self, point: Dict[str, Any]) -> None:
        name = point["name"]
        tags = point.get("tags", {})
        epoch = parse_ts(point["ts"])
        value = point["value"]

        tag_key = tuple(sorted(tags.items()))
        series_key = (name, tag_key)

        series = self._series_map.get(series_key)
        if series is None:
            series = _Series(name, tags)
            self._series_map[series_key] = series
            if name not in self._by_metric:
                self._by_metric[name] = []
            self._by_metric[name].append(series)

        series.insert(epoch, value)
        self._total_points += 1

    def _replay(self) -> None:
        if self.path is None or not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict):
                        self.skipped_lines += 1
                        continue
                    point = validate_point(raw)
                except Exception:
                    self.skipped_lines += 1
                    continue
                self._insert_point(point)

    def write(self, points: List[Dict[str, Any]]) -> int:
        """Validate every point, then append to memory and disk."""
        if not isinstance(points, list):
            raise ValidationError("points: must be a list")

        validated: List[Dict[str, Any]] = []
        for p in points:
            validated.append(validate_point(p))

        for p in validated:
            self._insert_point(p)

        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as f:
                for p in validated:
                    f.write(json.dumps(p) + "\n")
                f.flush()

        return len(validated)

    def query(
        self,
        name: str,
        start: str,
        end: str,
        tags: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """Query points for a metric across a time range and optional tag filters."""
        start_epoch = parse_ts(start, field="start")
        end_epoch = parse_ts(end, field="end")

        if tags is not None and not isinstance(tags, dict):
            raise ValidationError("tags: must be a dict")

        if name not in self._by_metric:
            return []

        matching_series: List[_Series] = []
        for series in self._by_metric[name]:
            if tags:
                if all(series.tags.get(k) == v for k, v in tags.items()):
                    matching_series.append(series)
            else:
                matching_series.append(series)

        collected: List[Tuple[int, Dict[str, Any]]] = []
        for series in matching_series:
            start_idx = bisect.bisect_left(series.timestamps, start_epoch)
            end_idx = bisect.bisect_left(series.timestamps, end_epoch)
            if start_idx < end_idx:
                for i in range(start_idx, end_idx):
                    collected.append((
                        series.timestamps[i],
                        {
                            "name": series.name,
                            "value": series.values[i],
                            "ts": format_ts(series.timestamps[i]),
                            "tags": dict(series.tags),
                        },
                    ))

        collected.sort(key=lambda item: item[0])
        return [item[1] for item in collected]

    def __len__(self) -> int:
        """Return the total number of points stored."""
        return self._total_points
