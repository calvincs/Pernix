"""Pernix — Metrics reporter (Phase 4b).

Read-only aggregation over post_mortems, sessions, and tool/skill performance. No LLM,
no writes. Answers "is the feedback loop working?" from existing data.

Exposed as pure functions (testable) plus a format_report() helper that
prints a plaintext summary for the CLI in scripts/metrics.py.

Intentionally minimal. Add new metrics as questions arise — resist the
urge to track everything. Every metric here should map to a decision.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from db.database import connect_sessions


@dataclass
class MetricsReport:
    """Aggregate metrics over a time window."""

    window_start: str
    window_end: str
    post_mortems_total: int = 0
    verdicts: dict = field(default_factory=dict)  # verdict -> count
    failure_causes: dict = field(default_factory=dict)  # cause -> count (non-pass only)
    viability: dict = field(default_factory=dict)  # verified/unverified/pending -> count
    execution_modes: dict = field(default_factory=dict)  # mode -> count
    deliverables_total: int = 0
    deliverables_status: dict = field(default_factory=dict)  # status -> count
    reflect_latency_p50: float = 0.0
    reflect_latency_p95: float = 0.0
    confidence_mean_by_verdict: dict = field(default_factory=dict)
    signals_total: int = 0
    signals_by_type: dict = field(default_factory=dict)  # type -> count
    signals_failures: int = 0  # tools/skills with at least one failure

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "post_mortems_total": self.post_mortems_total,
            "verdicts": dict(self.verdicts),
            "failure_causes": dict(self.failure_causes),
            "viability": dict(self.viability),
            "execution_modes": dict(self.execution_modes),
            "deliverables_total": self.deliverables_total,
            "deliverables_status": dict(self.deliverables_status),
            "reflect_latency_p50": self.reflect_latency_p50,
            "reflect_latency_p95": self.reflect_latency_p95,
            "confidence_mean_by_verdict": dict(self.confidence_mean_by_verdict),
            "signals_total": self.signals_total,
            "signals_by_type": dict(self.signals_by_type),
            "signals_failures": self.signals_failures,
        }


def _default_window(days: int) -> tuple[str, str]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def compute(since_iso: str | None = None, until_iso: str | None = None, days: int = 7) -> MetricsReport:
    """Build a MetricsReport over the time window.

    If neither since nor until is given, defaults to last `days` days.
    Windows are inclusive of start, exclusive of end.
    """
    if since_iso is None or until_iso is None:
        s, e = _default_window(days)
        since_iso = since_iso or s
        until_iso = until_iso or e

    report = MetricsReport(window_start=since_iso, window_end=until_iso)

    with connect_sessions() as conn:
        # --- post_mortems in window ---
        rows = conn.execute(
            """SELECT verdict, failure_cause, scout_viability, execution_mode,
                      confidence, reflect_latency_ms, payload_json
               FROM post_mortems
               WHERE created_at >= ? AND created_at < ?""",
            (since_iso, until_iso),
        ).fetchall()

        report.post_mortems_total = len(rows)

        verdicts = Counter()
        causes = Counter()
        viability = Counter()
        modes = Counter()
        deliv_status = Counter()
        deliv_total = 0
        latencies: list[int] = []
        conf_by_verdict: dict[str, list[float]] = {}

        for r in rows:
            v = r["verdict"] or "unknown"
            verdicts[v] += 1
            if v != "pass":
                causes[r["failure_cause"] or "none"] += 1
            if r["scout_viability"]:
                viability[r["scout_viability"]] += 1
            if r["execution_mode"]:
                modes[r["execution_mode"]] += 1
            if r["reflect_latency_ms"] is not None:
                latencies.append(int(r["reflect_latency_ms"]))
            try:
                conf = float(r["confidence"]) if r["confidence"] is not None else None
            except (TypeError, ValueError):
                conf = None
            if conf is not None:
                conf_by_verdict.setdefault(v, []).append(conf)

            # Deliverables from payload
            try:
                payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
            except (ValueError, TypeError):
                payload = {}
            for d in payload.get("deliverables") or []:
                if isinstance(d, dict):
                    deliv_status[d.get("status", "unknown")] += 1
                    deliv_total += 1

        report.verdicts = dict(verdicts)
        report.failure_causes = dict(causes)
        report.viability = dict(viability)
        report.execution_modes = dict(modes)
        report.deliverables_total = deliv_total
        report.deliverables_status = dict(deliv_status)
        if latencies:
            report.reflect_latency_p50 = float(statistics.median(latencies))
            report.reflect_latency_p95 = float(_percentile(latencies, 95))
        report.confidence_mean_by_verdict = {
            v: round(statistics.mean(cs), 3) for v, cs in conf_by_verdict.items() if cs
        }

        # --- Performance snapshot (tool/skill counters, not windowed) ---
        sig_rows = conn.execute("SELECT * FROM scout_signals WHERE signal_type IN ('tool', 'skill')").fetchall()
        report.signals_total = len(sig_rows)
        by_type = Counter()
        for s in sig_rows:
            by_type[s["signal_type"]] += 1
            if (s["failures"] or 0) > 0:
                report.signals_failures += 1
        report.signals_by_type = dict(by_type)

    return report


def _percentile(values: list[int | float], p: int) -> float:
    """Simple percentile (no interpolation). Values must be non-empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return float(ordered[f])
    # linear interpolation
    return float(ordered[f] + (ordered[c] - ordered[f]) * (k - f))


def format_report(report: MetricsReport) -> str:
    """Human-readable plaintext summary for the CLI."""
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append(f"Pernix Metrics: {report.window_start}  →  {report.window_end}")
    lines.append("=" * 64)
    lines.append("")

    lines.append(f"POST-MORTEMS: {report.post_mortems_total}")
    if report.post_mortems_total:
        lines.append("  Verdicts:       " + _fmt_dist(report.verdicts, report.post_mortems_total))
        if report.viability:
            lines.append("  Scout viability:" + _fmt_dist(report.viability, sum(report.viability.values()), pad=1))
        if report.execution_modes:
            lines.append(
                "  Execution mode: " + _fmt_dist(report.execution_modes, sum(report.execution_modes.values()), pad=1)
            )
        if report.failure_causes:
            lines.append(
                "  Failure causes: " + _fmt_dist(report.failure_causes, sum(report.failure_causes.values()), pad=1)
            )
        if report.confidence_mean_by_verdict:
            conf_str = ", ".join(f"{v}={c:.2f}" for v, c in sorted(report.confidence_mean_by_verdict.items()))
            lines.append(f"  Mean confidence by verdict: {conf_str}")
        lines.append(
            f"  Reflect latency: p50={report.reflect_latency_p50:.0f}ms " f"p95={report.reflect_latency_p95:.0f}ms"
        )
    lines.append("")

    lines.append(f"DELIVERABLES: {report.deliverables_total}")
    if report.deliverables_total:
        lines.append("  Status:         " + _fmt_dist(report.deliverables_status, report.deliverables_total))
    lines.append("")

    lines.append(f"SIGNALS (tool/skill observed performance): {report.signals_total}")
    if report.signals_total:
        lines.append("  By type:        " + _fmt_dist(report.signals_by_type, report.signals_total))
        lines.append(f"  With failures: {report.signals_failures}")
    lines.append("")
    return "\n".join(lines)


def _fmt_dist(dist: dict, total: int, pad: int = 0) -> str:
    if total <= 0:
        return "(none)"
    parts = []
    for k in sorted(dist.keys()):
        v = dist[k]
        pct = 100 * v / total
        parts.append(f"{k}={v} ({pct:.0f}%)")
    return (" " * pad) + ", ".join(parts)
