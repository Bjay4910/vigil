"""Anomaly detector: pull points from the store and flag unusual values."""

import json
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

def to_frame(points: List[Dict[str, Any]]) -> pd.DataFrame:
    """Turn /query points into a Dataframe with one 'series' key per tag combo"""

    df = pd.DataFrame(points, columns=["name", "value", "ts", "tags"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["series"] = df["tags"].apply(lambda t: json.dumps(t, sort_keys=True))
    return df

def fetch(url: str, name: str, start: str, end: str, tags: Optional[str] = None) -> pd.DataFrame:
    """Get /query and return the points as a DataFrame"""
    params = {"name": name, "from": start, "to": end}
    if tags:
        params["tags"] = tags
    full_url = url.rstrip("/") + "/query?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full_url) as resp:
        points = json.loads(resp.read())["points"]
    return to_frame(points)

def _flag_series(df: pd.DataFrame, make_window: Callable, threshold: float, rule: str) -> List[Dict[str, Any]]:
    """Score each series against its own past and return flags above the threshold."""
    flags: List[Dict[str, Any]] = []
    for (name, _), group in df.groupby(["name", "series"]):
        group = group.sort_values("ts")
        values = group["value"]

        past = make_window(values)
        mean = past.mean().shift(1)
        std = past.std().shift(1)

        z = (values - mean) / std
        hits = group[(z.abs() > threshold) & (std > 0)]

        for idx, row in hits.iterrows():
            flags.append({
                "name": name,
                "tags": row["tags"],
                "ts": row["ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "value": row["value"],
                "score": round(float(z[idx]), 2),
                "rule": rule,
            })
    return flags


def zscore_flags(df: pd.DataFrame, window: int = 60, threshold: float = 3.0) -> List[Dict[str, Any]]:
    """Flag points whose rolling z-score is above the threshold."""
    return _flag_series(df, lambda v: v.rolling(window, min_periods=window), threshold, "zscore")


def ewma_flags(
    df: pd.DataFrame, alpha: float = 0.05, warmup: int = 60, threshold: float = 3.0
) -> List[Dict[str, Any]]:
    """Flag points far from the exponentially weighted mean of the points before them."""
    return _flag_series(df, lambda v: v.ewm(alpha=alpha, min_periods=warmup), threshold, "ewma")