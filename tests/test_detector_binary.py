"""Tests for the binary_flags detector rule."""

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from detection.detector import binary_flags, to_frame

START = "2026-10-15T00:00:00Z"
ACME_TAGS = {"client": "acme"}


def _make_points(
    values: List[float],
    name: str = "uptime_up",
    tags: Optional[Dict[str, str]] = None,
    start: str = START,
) -> List[Dict[str, Any]]:
    base = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    tag_dict = dict(tags) if tags is not None else dict(ACME_TAGS)
    points: List[Dict[str, Any]] = []
    for i, val in enumerate(values):
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        points.append({
            "name": name,
            "value": val,
            "ts": ts,
            "tags": dict(tag_dict),
        })
    return points


def test_all_ones_gives_no_flags() -> None:
    df = to_frame(_make_points([1.0] * 20))
    assert binary_flags(df) == []


def test_single_zero_gives_one_flag_with_score_one() -> None:
    df = to_frame(_make_points([1.0, 1.0, 0.0, 1.0, 1.0]))
    flags = binary_flags(df)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["ts"] == "2026-10-15T00:02:00Z"
    assert flag["score"] == 1.0
    assert flag["value"] == 0.0
    assert flag["rule"] == "binary"


def test_run_of_five_zeros_gives_one_flag_with_score_five() -> None:
    df = to_frame(_make_points([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]))
    flags = binary_flags(df)
    assert len(flags) == 1
    assert flags[0]["ts"] == "2026-10-15T00:01:00Z"
    assert flags[0]["score"] == 5.0


def test_two_separate_outages_give_two_flags_in_time_order() -> None:
    df = to_frame(_make_points([1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 1.0]))
    flags = binary_flags(df)
    assert len(flags) == 2
    assert flags[0]["ts"] == "2026-10-15T00:01:00Z"
    assert flags[0]["score"] == 2.0
    assert flags[1]["ts"] == "2026-10-15T00:05:00Z"
    assert flags[1]["score"] == 1.0
    assert flags[0]["ts"] < flags[1]["ts"]


def test_series_starting_with_zero_flagged_at_first_point() -> None:
    df = to_frame(_make_points([0.0, 0.0, 1.0, 1.0]))
    flags = binary_flags(df)
    assert len(flags) == 1
    assert flags[0]["ts"] == "2026-10-15T00:00:00Z"
    assert flags[0]["score"] == 2.0


def test_all_zeros_flagged_at_first_point_with_series_length() -> None:
    df = to_frame(_make_points([0.0] * 8))
    flags = binary_flags(df)
    assert len(flags) == 1
    assert flags[0]["ts"] == "2026-10-15T00:00:00Z"
    assert flags[0]["score"] == 8.0


def test_non_binary_series_skipped_with_no_flags() -> None:
    df_half = to_frame(_make_points([1.0, 0.5, 0.0, 1.0]))
    assert binary_flags(df_half) == []

    df_nan = to_frame(_make_points([1.0, float("nan"), 0.0, 1.0]))
    assert binary_flags(df_nan) == []

    df_page_load = to_frame(
        _make_points([820.0, 840.0, 0.0, 850.0], name="page_load_ms")
    )
    assert binary_flags(df_page_load) == []


def test_two_series_with_different_tags_handled_independently() -> None:
    pts_a = _make_points(
        [1.0, 1.0, 0.0, 0.0, 1.0],
        tags={"client": "acme", "site": "site-a"},
    )
    pts_b = _make_points(
        [1.0, 0.0, 1.0, 1.0, 1.0],
        tags={"client": "acme", "site": "site-b"},
    )
    df = to_frame(pts_a + pts_b)
    flags = binary_flags(df)
    assert len(flags) == 2
    assert flags[0]["tags"] == {"client": "acme", "site": "site-b"}
    assert flags[0]["ts"] == "2026-10-15T00:01:00Z"
    assert flags[0]["score"] == 1.0
    assert flags[1]["tags"] == {"client": "acme", "site": "site-a"}
    assert flags[1]["ts"] == "2026-10-15T00:02:00Z"
    assert flags[1]["score"] == 2.0


def test_empty_dataframe_returns_empty_list() -> None:
    empty_df = to_frame([])
    assert binary_flags(empty_df) == []


def test_flag_fields_timestamp_format_and_json_serializable() -> None:
    df = to_frame(_make_points([1.0, 0.0, 0.0, 1.0, 0.0]))
    flags = binary_flags(df)
    assert len(flags) == 2
    ts_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    for flag in flags:
        assert set(flag.keys()) == {"name", "tags", "ts", "value", "score", "rule"}
        assert ts_re.match(flag["ts"])
        assert type(flag["value"]) is float
        assert type(flag["score"]) is float
        assert type(flag["name"]) is str
        assert type(flag["rule"]) is str
        assert type(flag["tags"]) is dict
    serialized = json.dumps(flags)
    assert isinstance(serialized, str)


def test_input_dataframe_is_not_modified() -> None:
    # Provide points in reverse time order so sorting inside binary_flags would reveal mutation
    pts = list(reversed(_make_points([1.0, 0.0, 0.0, 1.0])))
    df = to_frame(pts)
    before = df.copy(deep=True)
    flags = binary_flags(df)
    assert len(flags) == 1
    pd.testing.assert_frame_equal(df, before)

