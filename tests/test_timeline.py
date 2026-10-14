"""Tests for timeline assembly: since-parsing, sorting, summaries."""

from datetime import datetime, timedelta, timezone

import pytest

from k8s_timeline.timeline import build_timeline, parse_since, summarize

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def entry(ts, level="info", reason="R", kind="Pod", name="p"):
    return {"timestamp": ts, "level": level, "reason": reason,
            "kind": kind, "name": name, "category": "event",
            "namespace": "ns", "message": "m", "count": 1}


def test_parse_since_relative():
    assert parse_since("30m", NOW) == NOW - timedelta(minutes=30)
    assert parse_since("2h", NOW) == NOW - timedelta(hours=2)
    assert parse_since("1d", NOW) == NOW - timedelta(days=1)
    assert parse_since("1w", NOW) == NOW - timedelta(weeks=1)


def test_parse_since_rfc3339():
    assert parse_since("2026-10-14T10:00:00Z", NOW) == \
        datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)


def test_parse_since_invalid():
    with pytest.raises(ValueError):
        parse_since("yesterday", NOW)


def test_build_timeline_sorts_and_drops_missing():
    e1 = entry(NOW)
    e2 = entry(NOW - timedelta(hours=1))
    e3 = entry(None)
    out = build_timeline([e1, e3, e2])
    assert [e["timestamp"] for e in out] == [e2["timestamp"], e1["timestamp"]]


def test_summarize_counts():
    entries = build_timeline([
        entry(NOW - timedelta(minutes=5), level="error", reason="BackOff",
              kind="Pod", name="api-1"),
        entry(NOW - timedelta(minutes=3), level="error", reason="BackOff",
              kind="Pod", name="api-1"),
        entry(NOW, level="info", reason="Scheduled", kind="Pod", name="api-2"),
    ])
    s = summarize(entries)
    assert s["total_entries"] == 3
    assert s["by_level"] == {"error": 2, "info": 1}
    assert s["top_reasons"][0] == {"reason": "BackOff", "count": 2}
    assert s["top_objects"][0] == {"object": "Pod/api-1", "count": 2}
    assert s["span"]["start"].startswith("2026-10-14T11:55")


def test_summarize_empty():
    s = summarize([])
    assert s["total_entries"] == 0
    assert s["span"] is None
