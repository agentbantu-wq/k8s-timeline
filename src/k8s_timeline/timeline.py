"""Timeline assembly: normalize, sort, summarize."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def parse_since(value: str, now: datetime) -> datetime:
    """Parse --since values: '30m', '2h', '1d', '1w', or an RFC3339 timestamp."""
    text = value.strip()
    m = re.fullmatch(r"(\d+)([mhdw])", text)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount),
                 "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
        return now - delta
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            f"cannot parse --since={value!r}: use 30m/2h/1d/1w or an RFC3339 timestamp"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_timeline(entries: list[dict]) -> list[dict]:
    """Drop entries without timestamps and sort chronologically (stable)."""
    valid = [e for e in entries if e.get("timestamp") is not None]
    valid.sort(key=lambda e: e["timestamp"])
    return valid


def summarize(entries: list[dict]) -> dict:
    """Aggregate counts a human (or an LLM) can skim before reading the timeline."""
    by_level: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_object: dict[str, int] = {}
    for e in entries:
        by_level[e.get("level", "?")] = by_level.get(e.get("level", "?"), 0) + 1
        by_reason[e.get("reason", "?")] = by_reason.get(e.get("reason", "?"), 0) + 1
        key = f"{e.get('kind', '?')}/{e.get('name', '?')}"
        by_object[key] = by_object.get(key, 0) + 1
    top_reasons = sorted(by_reason.items(), key=lambda kv: kv[1], reverse=True)[:10]
    top_objects = sorted(by_object.items(), key=lambda kv: kv[1], reverse=True)[:10]
    span = None
    if entries:
        span = {
            "start": entries[0]["timestamp"].isoformat(),
            "end": entries[-1]["timestamp"].isoformat(),
        }
    return {
        "total_entries": len(entries),
        "by_level": by_level,
        "top_reasons": [{"reason": r, "count": c} for r, c in top_reasons],
        "top_objects": [{"object": o, "count": c} for o, c in top_objects],
        "span": span,
    }
