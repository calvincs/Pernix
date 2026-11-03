"""Watch recorded automatic failover usage, never infer routing from model names.

The ledger covers streaming agent calls, not all background/provider work.
Historical rows without route provenance are unknown and cannot trigger an alert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_WINDOW_HOURS = 24


def evaluate_fallback_burn(
    rows: list[dict],
    fallback_model: str,
    share_threshold: float,
    min_tokens: int,
) -> dict | None:
    """Threshold actual failover tokens against all recorded streaming tokens."""
    if not fallback_model or share_threshold <= 0:
        return None
    total = sum(int(r.get("total") or 0) for r in rows)
    if total < max(int(min_tokens), 1):
        return None
    routes = [r for r in rows if r.get("routing_reason") == "automatic_failover"]
    burned = sum(int(r.get("total") or 0) for r in routes)
    if burned <= 0 or burned / total < share_threshold:
        return None
    return {
        "model": ", ".join(sorted({str(r["model"]) for r in routes})),
        "share": burned / total,
        "tokens": burned,
        "total_tokens": total,
        "calls": sum(int(r.get("calls") or 0) for r in routes),
        "window_hours": _WINDOW_HOURS,
        "sessions": sorted({r["session_id"] for r in routes if r.get("session_id")}),
        "reasons": sorted({r.get("failure_reason") or "unknown" for r in routes}),
        "requested_models": sorted({r["requested_model"] for r in routes if r.get("requested_model")}),
    }


def check_fallback_burn() -> dict | None:
    """Read an exact UTC window. Errors propagate so they cannot resolve an incident."""
    from config import settings
    from db import models as db

    if not settings.fallback_model or float(settings.fallback_burn_alert_share or 0) <= 0:
        return None
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(hours=_WINDOW_HOURS)
    rows = db.token_usage_by_model_since(start.isoformat(), end.isoformat())
    finding = evaluate_fallback_burn(
        rows,
        settings.fallback_model,
        float(settings.fallback_burn_alert_share),
        int(settings.fallback_burn_min_tokens or 0),
    )
    if finding:
        finding.update(window_start=start.isoformat(), window_end=end.isoformat())
    return finding


def format_fallback_finding(finding: dict) -> str:
    sessions = finding["sessions"]
    visible = ", ".join(sessions[:10])
    if len(sessions) > 10:
        visible += f" (+{len(sessions) - 10} more)"
    return (
        f"Automatic failover served {finding['tokens']:,} of {finding['total_tokens']:,} recorded "
        f"streaming tokens ({finding['share']:.0%}) over {finding['calls']} calls. "
        f"UTC window: {finding['window_start']} to {finding['window_end']}. "
        f"Requested models: {', '.join(finding['requested_models']) or 'unknown'}. "
        f"Served models: {finding['model']}. "
        f"Recorded failure categories: {', '.join(finding['reasons'])}. "
        f"Affected sessions ({len(sessions)}): {visible or 'unknown'}. "
        "Explicit model selections are excluded from failover totals. Background calls are not included "
        "in this ledger; historical routes without attribution remain unknown. "
        "This window does not establish a current provider outage."
    )
