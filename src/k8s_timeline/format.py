"""Output formatters: JSON, Markdown, plain text, and postmortem-copilot export."""

from __future__ import annotations

import json


def entry_to_dict(entry: dict) -> dict:
    return {
        "timestamp": entry["timestamp"].isoformat(),
        "category": entry.get("category", ""),
        "kind": entry.get("kind", ""),
        "name": entry.get("name", ""),
        "namespace": entry.get("namespace", ""),
        "level": entry.get("level", ""),
        "reason": entry.get("reason", ""),
        "message": entry.get("message", ""),
        "count": entry.get("count", 1),
    }


def to_json(entries: list[dict], summary: dict) -> str:
    return json.dumps(
        {"summary": summary, "entries": [entry_to_dict(e) for e in entries]},
        indent=2,
    )


_LEVEL_EMOJI = {"error": "🔴", "warning": "🟡", "info": "🔵"}


def to_markdown(entries: list[dict], summary: dict, namespace: str | None) -> str:
    lines = ["# Kubernetes incident timeline"]
    if namespace:
        lines.append(f"_Namespace: `{namespace}`_")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Entries: **{summary['total_entries']}**")
    for level, count in sorted(summary["by_level"].items()):
        lines.append(f"- {level}: **{count}**")
    if summary["span"]:
        lines.append(f"- Span: `{summary['span']['start']}` → `{summary['span']['end']}`")
    lines.append("")
    lines.append("## Top signals")
    for item in summary["top_reasons"][:8]:
        lines.append(f"- `{item['reason']}` ×{item['count']}")
    lines.append("")
    lines.append("## Timeline")
    for e in entries:
        emoji = _LEVEL_EMOJI.get(e.get("level"), "⚪")
        ts = e["timestamp"].strftime("%H:%M:%S")
        where = f"{e.get('kind')}/{e.get('name')}"
        lines.append(f"- {emoji} `{ts}` **{where}** — {e.get('reason')}: {e.get('message')}")
    lines.append("")
    return "\n".join(lines)


def to_text(entries: list[dict], summary: dict) -> str:
    width = 8
    lines = [
        f"entries={summary['total_entries']} " +
        " ".join(f"{k}={v}" for k, v in sorted(summary["by_level"].items())),
        "-" * 100,
    ]
    for e in entries:
        ts = e["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        level = e.get("level", "?").upper().ljust(width)
        where = f"{e.get('kind')}/{e.get('name')}"
        lines.append(f"{ts} {level} {where}: {e.get('reason')}: {e.get('message')}")
    return "\n".join(lines)


def to_postmortem_copilot(entries: list[dict], service: str,
                          severity: str = "SEV-3") -> str:
    """Export in postmortem-copilot's timeline format.

    Lets the timeline feed straight into
    https://github.com/agentbantu-wq/postmortem-copilot :
        k8s-timeline build -n prod --pmc -o timeline.json
        pmc generate timeline.json
    """
    pmc_events = [
        {
            "timestamp": e["timestamp"].isoformat(),
            "source": f"k8s:{e.get('category', 'event')}",
            "message": f"[{e.get('kind')}/{e.get('name')}] "
                       f"{e.get('reason')}: {e.get('message')}",
        }
        for e in entries
    ]
    return json.dumps(
        {"service": service, "severity": severity, "events": pmc_events},
        indent=2,
    )
