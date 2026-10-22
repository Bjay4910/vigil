"""Tests for the detector backtest."""

from detection.backtest import THRESHOLDS, run, score


def test_score_counts_hits_misses_and_false_alarms():
    truth = {"a", "b", "c", "d"}
    found = {"a", "b", "x"}
    result = score(truth, found)
    assert result["caught"] == 2
    assert result["false_alarms"] == 1
    assert result["precision"] == 2 / 3
    assert result["recall"] == 2 / 4


def test_score_handles_no_flags():
    result = score({"a"}, set())
    assert result["precision"] == 0.0
    assert result["recall"] == 0.0


def test_run_covers_every_rule_and_threshold():
    rows = run()
    assert len(rows) == 2 * len(THRESHOLDS)
    assert all(0.0 <= r["precision"] <= 1.0 and 0.0 <= r["recall"] <= 1.0 for r in rows)
