"""Tests for time-series store engine."""

import pytest
from store import TimeSeriesStore, ValidationError


def test_query_range_start_inclusive_end_exclusive():
    store = TimeSeriesStore()
    points = [
        {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"},
        {"name": "page_load_ms", "value": 200, "ts": "2026-10-06T11:00:00Z"},
        {"name": "page_load_ms", "value": 300, "ts": "2026-10-06T12:00:00Z"},
    ]
    store.write(points)

    res = store.query(
        "page_load_ms",
        start="2026-10-06T10:00:00Z",
        end="2026-10-06T12:00:00Z",
    )
    assert len(res) == 2
    assert res[0]["ts"] == "2026-10-06T10:00:00Z"
    assert res[0]["value"] == 100.0
    assert res[1]["ts"] == "2026-10-06T11:00:00Z"
    assert res[1]["value"] == 200.0


def test_points_written_out_of_order_sorted_oldest_first():
    store = TimeSeriesStore()
    points = [
        {"name": "page_load_ms", "value": 300, "ts": "2026-10-06T14:00:00Z"},
        {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"},
        {"name": "page_load_ms", "value": 200, "ts": "2026-10-06T12:00:00Z"},
    ]
    store.write(points)

    res = store.query(
        "page_load_ms",
        start="2026-10-06T00:00:00Z",
        end="2026-10-07T00:00:00Z",
    )
    assert len(res) == 3
    assert [p["ts"] for p in res] == [
        "2026-10-06T10:00:00Z",
        "2026-10-06T12:00:00Z",
        "2026-10-06T14:00:00Z",
    ]
    assert [p["value"] for p in res] == [100.0, 200.0, 300.0]


def test_tag_filter_subset_matching_and_separate_series():
    store = TimeSeriesStore()
    points = [
        {
            "name": "page_load_ms",
            "value": 100,
            "ts": "2026-10-06T10:00:00Z",
            "tags": {"client": "acme", "env": "prod"},
        },
        {
            "name": "page_load_ms",
            "value": 200,
            "ts": "2026-10-06T11:00:00Z",
            "tags": {"client": "acme", "env": "stage"},
        },
        {
            "name": "page_load_ms",
            "value": 300,
            "ts": "2026-10-06T12:00:00Z",
            "tags": {"client": "acme", "env": "prod", "site": "acme.example"},
        },
        {
            "name": "page_load_ms",
            "value": 400,
            "ts": "2026-10-06T13:00:00Z",
            "tags": {"client": "globex", "env": "prod"},
        },
    ]
    store.write(points)

    acme_res = store.query(
        "page_load_ms",
        start="2026-10-06T00:00:00Z",
        end="2026-10-07T00:00:00Z",
        tags={"client": "acme"},
    )
    assert len(acme_res) == 3
    assert [p["value"] for p in acme_res] == [100.0, 200.0, 300.0]

    acme_prod_res = store.query(
        "page_load_ms",
        start="2026-10-06T00:00:00Z",
        end="2026-10-07T00:00:00Z",
        tags={"client": "acme", "env": "prod"},
    )
    assert len(acme_prod_res) == 2
    assert [p["value"] for p in acme_prod_res] == [100.0, 300.0]

    globex_res = store.query(
        "page_load_ms",
        start="2026-10-06T00:00:00Z",
        end="2026-10-07T00:00:00Z",
        tags={"client": "globex"},
    )
    assert len(globex_res) == 1
    assert globex_res[0]["value"] == 400.0


def test_unknown_metric_returns_empty_list():
    store = TimeSeriesStore()
    store.write([{"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"}])
    res = store.query(
        "unknown_metric",
        start="2026-10-06T00:00:00Z",
        end="2026-10-07T00:00:00Z",
    )
    assert res == []


@pytest.mark.parametrize(
    "raw_point,expected_field",
    [
        (
            {"name": "Page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"},
            "name",
        ),
        (
            {"name": "page_load_ms", "value": True, "ts": "2026-10-06T10:00:00Z"},
            "value",
        ),
        (
            {"name": "page_load_ms", "value": "100", "ts": "2026-10-06T10:00:00Z"},
            "value",
        ),
        (
            {"name": "page_load_ms", "value": float("nan"), "ts": "2026-10-06T10:00:00Z"},
            "value",
        ),
        (
            {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00"},
            "ts",
        ),
        (
            {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00+00:00"},
            "ts",
        ),
        (
            {
                "name": "page_load_ms",
                "value": 100,
                "ts": "2026-10-06T10:00:00Z",
                "tags": {"region": "us-east"},
            },
            "tags",
        ),
        (
            {
                "name": "page_load_ms",
                "value": 100,
                "ts": "2026-10-06T10:00:00Z",
                "tags": {"client": "Acme"},
            },
            "tags",
        ),
        (
            {
                "name": "page_load_ms",
                "value": 100,
                "ts": "2026-10-06T10:00:00Z",
                "tags": "not-a-dict",
            },
            "tags",
        ),
    ],
)
def test_validation_errors_contain_field_name(raw_point, expected_field):
    store = TimeSeriesStore()
    with pytest.raises(ValidationError) as exc_info:
        store.write([raw_point])
    assert expected_field in str(exc_info.value)
    assert str(exc_info.value).startswith(expected_field)


def test_query_invalid_start_and_end_messages():
    store = TimeSeriesStore()
    with pytest.raises(ValidationError) as exc_start:
        store.query("page_load_ms", start="bad_start", end="2026-10-06T12:00:00Z")
    assert str(exc_start.value).startswith("start")

    with pytest.raises(ValidationError) as exc_end:
        store.query("page_load_ms", start="2026-10-06T10:00:00Z", end="bad_end")
    assert str(exc_end.value).startswith("end")


def test_batch_with_one_bad_point_writes_nothing():
    store = TimeSeriesStore()
    batch = [
        {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"},
        {"name": "INVALID_NAME", "value": 200, "ts": "2026-10-06T11:00:00Z"},
    ]
    with pytest.raises(ValidationError):
        store.write(batch)
    assert len(store) == 0
    res = store.query(
        "page_load_ms",
        start="2026-10-06T00:00:00Z",
        end="2026-10-07T00:00:00Z",
    )
    assert res == []


def test_persistence_replays_matching_points(tmp_path):
    db_file = tmp_path / "vigil_store.jsonl"
    store1 = TimeSeriesStore(path=str(db_file))
    points = [
        {
            "name": "page_load_ms",
            "value": 150,
            "ts": "2026-10-06T10:00:00Z",
            "tags": {"client": "acme"},
        },
        {
            "name": "page_load_ms",
            "value": 250,
            "ts": "2026-10-06T11:00:00Z",
            "tags": {"client": "globex"},
        },
    ]
    assert store1.write(points) == 2
    res1 = store1.query(
        "page_load_ms",
        start="2026-10-06T00:00:00Z",
        end="2026-10-07T00:00:00Z",
    )

    store2 = TimeSeriesStore(path=str(db_file))
    assert len(store2) == 2
    res2 = store2.query(
        "page_load_ms",
        start="2026-10-06T00:00:00Z",
        end="2026-10-07T00:00:00Z",
    )
    assert res1 == res2


def test_corrupt_or_truncated_last_line_skipped(tmp_path):
    db_file = tmp_path / "corrupt_store.jsonl"
    store1 = TimeSeriesStore(path=str(db_file))
    valid_points = [
        {
            "name": "page_load_ms",
            "value": 100,
            "ts": "2026-10-06T10:00:00Z",
            "tags": {"client": "acme"},
        },
        {
            "name": "page_load_ms",
            "value": 200,
            "ts": "2026-10-06T11:00:00Z",
            "tags": {"client": "globex"},
        },
    ]
    store1.write(valid_points)

    with open(db_file, "a", encoding="utf-8") as f:
        f.write('{"name": "page_load_ms", "value": 300, "ts"\n')

    store2 = TimeSeriesStore(path=str(db_file))
    assert store2.skipped_lines == 1
    assert len(store2) == 2
    res = store2.query(
        "page_load_ms",
        start="2026-10-06T00:00:00Z",
        end="2026-10-07T00:00:00Z",
    )
    assert len(res) == 2
    assert [p["value"] for p in res] == [100.0, 200.0]


def test_len_correct_after_several_writes():
    store = TimeSeriesStore()
    assert len(store) == 0

    store.write([
        {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"}
    ])
    assert len(store) == 1

    store.write([
        {"name": "page_load_ms", "value": 200, "ts": "2026-10-06T11:00:00Z"},
        {"name": "uptime_up", "value": 1, "ts": "2026-10-06T11:00:00Z"},
    ])
    assert len(store) == 3

    store.write([
        {
            "name": "api_response_ms",
            "value": 45,
            "ts": "2026-10-06T12:00:00Z",
            "tags": {"client": "acme"},
        },
        {
            "name": "api_response_ms",
            "value": 55,
            "ts": "2026-10-06T13:00:00Z",
            "tags": {"client": "globex"},
        },
    ])
    assert len(store) == 5
