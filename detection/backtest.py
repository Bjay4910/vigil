"""Backtest: score the detectors against spikes we injected ourselves.

Run it:
    python3 -m detection.backtest

Prints a results table and writes it to docs/backtest.md.
"""

import json
from typing import Any, Callable, Dict, List, Set, Tuple

import pandas as pd

from detection.detector import ewma_flags, to_frame, zscore_flags
from detection.generate import DEFAULT_SERIES, generate_series

START = "2026-10-06T00:00:00Z"
N_POINTS = 1440  # one day, one point per minute
SEEDS = [1, 2, 3, 4, 5]
THRESHOLDS = [3.0, 3.5, 4.0, 5.0]
RULES: Dict[str, Callable[..., List[Dict[str, Any]]]] = {"zscore": zscore_flags, "ewma": ewma_flags}

Key = Tuple[str, str, str]  # (name, series, ts)


def key(name: str, tags: Dict[str, str], ts: str) -> Key:
    """One comparable ID per point: which series, which minute."""
    return (name, json.dumps(tags, sort_keys=True), ts)


def build_data(seed: int) -> Tuple[pd.DataFrame, Set[Key]]:
    """Generate every default series for one seed. Return the data and the spike answer key."""
    points: List[Dict[str, Any]] = []
    truth: Set[Key] = set()
    for offset, (name, tags, baseline, noise) in enumerate(DEFAULT_SERIES):
        pts, spikes = generate_series(
            name, tags, START, N_POINTS, baseline=baseline, noise=noise, seed=seed * 10 + offset,
        )
        points.extend(pts)
        truth.update(key(name, tags, ts) for ts in spikes)
    return to_frame(points), truth


def score(truth: Set[tuple], found: Set[tuple]) -> Dict[str, float]:
    """Compare what the detector flagged with what we injected."""
    caught = len(truth & found)
    false_alarms = len(found - truth)
    precision = caught / len(found) if found else 0.0
    recall = caught / len(truth) if truth else 0.0
    return {"caught": caught, "false_alarms": false_alarms, "precision": precision, "recall": recall}


def run() -> List[Dict[str, Any]]:
    """Run every rule at every threshold over every seed and total the results."""
    datasets = [build_data(seed) for seed in SEEDS]
    rows: List[Dict[str, Any]] = []
    for rule, detect in RULES.items():
        for threshold in THRESHOLDS:
            truth_all: Set[tuple] = set()
            found_all: Set[tuple] = set()
            for i, (df, truth) in enumerate(datasets):
                flags = detect(df, threshold=threshold)
                # Prefix with the dataset number so seeds never collide.
                truth_all |= {(i,) + k for k in truth}
                found_all |= {(i,) + key(f["name"], f["tags"], f["ts"]) for f in flags}
            rows.append({"rule": rule, "threshold": threshold, **score(truth_all, found_all)})
    return rows


def to_markdown(rows: List[Dict[str, Any]]) -> str:
    """Format the results as a markdown table."""
    lines = [
        "| Rule | Threshold | Caught | False alarms | Precision | Recall |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['rule']} | {r['threshold']} | {r['caught']} | {r['false_alarms']} "
            f"| {r['precision']:.0%} | {r['recall']:.0%} |"
        )
    return "\n".join(lines)


def main() -> None:
    table = to_markdown(run())
    print(table)
    with open("docs/backtest.md", "w", encoding="utf-8") as f:
        f.write("# Detector backtest\n\n")
        f.write(f"{len(SEEDS)} seeds x {len(DEFAULT_SERIES)} series x {N_POINTS} points, ")
        f.write("3 injected spikes per series. Generated with `python3 -m detection.backtest`.\n\n")
        f.write(table + "\n")


if __name__ == "__main__":
    main()
