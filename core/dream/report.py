"""Pernix — Dream report: the periodic human-readable introspection artifact.

Written to <workspace>/dreams/DREAM-<date>.md so it appears in the existing
file explorer with zero UI work. Pure composition + a thin async writer.
No LLM involved. First enable only starts the clock — an empty store never
produces an empty report.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings
from db import models as db

logger = logging.getLogger("pernix.dream.report")

_STATUS_ORDER = ["refuted", "validated", "pending", "expired", "promoted", "archived"]
_STATUS_HEADINGS = {
    "refuted": "Refuted this period (falsification working as intended)",
    "validated": "Validated (await promotion gates)",
    "pending": "New hypotheses (untested)",
    "expired": "Expired (evidence moved or vanished)",
    "promoted": "Promoted",
    "archived": "Archived",
}


def compose_report(period_start: str, period_end: str, rows: list[dict]) -> str:
    """Pure: render report markdown from hypothesis rows updated in period."""
    counts: dict[str, int] = {}
    by_status: dict[str, list[dict]] = {}
    for r in rows:
        status = r.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
        by_status.setdefault(status, []).append(r)

    lines = [
        f"# Dream report — {period_end[:10]}",
        "",
        f"Period: {period_start[:19]} → {period_end[:19]}",
        "",
        "Counts: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"),
        "",
    ]
    for status in _STATUS_ORDER:
        rows_for = by_status.get(status)
        if not rows_for:
            continue
        lines.append(f"## {_STATUS_HEADINGS[status]}")
        lines.append("")
        for r in rows_for:
            conf = float(r.get("confidence") or 0.0)
            lines.append(f"- **[{r.get('kind')}]** {r.get('statement')} _(confidence {conf:.2f})_")
            validation = r.get("validation_json")
            if validation:
                try:
                    v = json.loads(validation)
                    method = v.get("method", "")
                    note = v.get("note", "")
                    if method or note:
                        lines.append(f"  - validation: {method} {note}".rstrip())
                except (TypeError, ValueError):
                    pass
        lines.append("")
    lines.append("---")
    lines.append(
        "_Hypotheses are not beliefs. Nothing here influences live behavior until it "
        "passes validation and the promotion gates (docs/dev/dream-plan.md)._"
    )
    lines.append("")
    return "\n".join(lines)


async def maybe_write_report() -> str | None:
    """Write the report if the interval elapsed and there is material.

    Returns the workspace-relative path when a report was written.
    """
    now = datetime.now(timezone.utc)
    last = db.get_snooze_state("dream_last_report")
    if not last:
        # First enable: start the clock, don't write an empty report.
        db.set_snooze_state("dream_last_report", now.isoformat())
        return None
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        db.set_snooze_state("dream_last_report", now.isoformat())
        return None
    if now - last_dt < timedelta(days=max(1, settings.dream_report_interval_days)):
        return None

    rows = [r for r in db.list_dream_hypotheses(limit=500) if (r.get("updated_at") or "") >= last]
    if not rows:
        # Quiet period: push the window forward without writing.
        db.set_snooze_state("dream_last_report", now.isoformat())
        return None

    content = compose_report(last, now.isoformat(), rows)
    rel_path = f"dreams/DREAM-{now.astimezone().strftime('%Y-%m-%d')}.md"
    abs_path = Path(settings.workspace_dir) / rel_path

    def _write() -> None:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

    await asyncio.to_thread(_write)

    stats = {"hypotheses_in_period": len(rows)}
    db.add_dream_report(last, now.isoformat(), rel_path, json.dumps(stats))
    db.set_snooze_state("dream_last_report", now.isoformat())
    logger.info("dream: wrote report %s (%d hypotheses)", rel_path, len(rows))
    from core.dream.journal import append as journal

    await journal(f"📝 Dream report written: {rel_path} ({len(rows)} hypotheses this period)")
    return rel_path
