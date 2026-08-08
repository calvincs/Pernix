"""Pernix — Candor intel: render calibrated reliability into scout-facing text.

Everything here runs on the bridge's executor thread against a live
CandorSystem and uses pure reads only (predict / raw_counts / questions plus
two direct index queries — the sanctioned pattern from Candor's own bench
code, confined to this module).

The brief is an EXCEPTION REPORT: healthy facts say nothing. And per the
fc329cb prompt lesson, it carries facts found in operational history — never
conclusions about what is missing or unconfigured.
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger("pernix.ext.candor")

# predict() sample count; below Candor's 1000-sample threshold this IS the
# budget, and the brief doesn't need full precision.
_PREDICT_BUDGET = 256
_MIN_OBSERVATIONS = 5
_DEGRADED_P = 0.55
_INTERESTING_CAVEATS = {"unstable", "under_specified", "regime_mixed", "constraint_tension"}
_MAX_LINES = 10
_TIME_BUDGET_S = 3.0
_CHAR_CAP = 1600

_HEADER = (
    "[OPERATIONAL INTEL] Calibrated reliability from logged outcome history "
    "(exception report — healthy tools are omitted; absence here means no known problem):"
)


def _observation_count(system, fact_id: str) -> int:
    try:
        return sum(n for (n, _k) in system.raw_counts(fact_id).values())
    except Exception:
        return 0


def _admitted_guards(system) -> dict:
    """Map target fact id → (ctx_key, value, regime_dependent)."""
    guards: dict = {}
    try:
        rows = system.index.query("SELECT body_json FROM candidates WHERE kind='guard' AND status='admitted'")
        for r in rows:
            body = json.loads(r["body_json"])
            g = (body.get("body") or {}).get("guards") or []
            fid = body.get("target_fact")
            if fid and g:
                key = str(g[0].get("var", "")).lstrip("?")
                guards[fid] = (key, g[0].get("value"), bool(body.get("regime_dependent")))
    except Exception as e:
        logger.debug("Candor guard query failed: %s", e)
    return guards


def build_brief(system) -> str | None:
    """Render degraded/flagged facts, admitted guards, and open questions."""
    t0 = time.monotonic()
    guards = _admitted_guards(system)

    fact_rows = list(
        system.index.query(
            "SELECT id, pred, args_json, stmt_type, dispersion_flag FROM facts "
            "WHERE valid_to IS NULL AND stmt_type IN ('frequency', 'crisp', 'categorical')"
        )
    )
    categorical_by_key: dict[tuple, dict] = {}
    binary_rows = []
    for r in fact_rows:
        args = json.loads(r["args_json"])
        if r["stmt_type"] == "categorical":
            categorical_by_key[(r["pred"], tuple(args))] = {"id": r["id"], "args": args}
        else:
            binary_rows.append((r, args))

    lines: list[str] = []
    degraded_tools: list[str] = []
    interaction_degraded = False
    for r, args in binary_rows:
        if len(lines) >= _MAX_LINES or time.monotonic() - t0 > _TIME_BUDGET_S:
            break
        stmt = {"pred": r["pred"], "args": args}
        n = _observation_count(system, r["id"])
        if n < _MIN_OBSERVATIONS:
            continue
        try:
            p = system.predict(stmt, budget=_PREDICT_BUDGET)
        except Exception:
            continue
        caveats = set(p.caveats) & _INTERESTING_CAVEATS
        guard = guards.get(r["id"])
        if p.p >= _DEGRADED_P and not caveats and not guard and not r["dispersion_flag"]:
            continue
        label = f"{r['pred']}({', '.join(str(a) for a in args)})"
        seg = f"- {label}: {p.p:.0%} success over {n} obs (CI {p.ci[0]:.0%}–{p.ci[1]:.0%})"
        if caveats:
            seg += f" [{', '.join(sorted(caveats))}]"
        if guard:
            key, value, regime = guard
            seg += f" — works when {key}={value}"
            if regime:
                seg += " (regime-dependent)"
        lines.append(seg)
        if r["pred"] == "tool_ok" and args and args[0] != "*" and p.p < _DEGRADED_P:
            degraded_tools.append(str(args[0]))
        if r["pred"] in ("no_clarification_needed", "first_response_sufficient") and p.p < _DEGRADED_P:
            interaction_degraded = True

    # For degraded tools, say WHICH failure dominates (open vocabulary — the
    # unknown mass is a real probability, reported as "unseen").
    for tool in degraded_tools[:3]:
        cat = categorical_by_key.get(("tool_failure_mode", (tool,)))
        if not cat or time.monotonic() - t0 > _TIME_BUDGET_S:
            continue
        try:
            c = system.predict({"pred": "tool_failure_mode", "args": [tool]}, budget=_PREDICT_BUDGET)
        except Exception:
            continue
        top = sorted(c.values.items(), key=lambda kv: kv[1].p, reverse=True)[:2]
        if not top:
            continue
        parts = [f"{name} {slice_.p:.0%}" for name, slice_ in top]
        parts.append(f"unseen {c.unknown.p:.0%}")
        lines.append(f"  ↳ {tool} failure modes: {', '.join(parts)}")

    # Degraded interaction quality → say WHICH friction dominates, same
    # open-vocabulary treatment as tool failure modes.
    if interaction_degraded and time.monotonic() - t0 <= _TIME_BUDGET_S:
        cat = categorical_by_key.get(("friction_mode", ("*",)))
        if cat:
            try:
                c = system.predict({"pred": "friction_mode", "args": ["*"]}, budget=_PREDICT_BUDGET)
                top = sorted(c.values.items(), key=lambda kv: kv[1].p, reverse=True)[:2]
                if top:
                    parts = [f"{name} {slice_.p:.0%}" for name, slice_ in top]
                    parts.append(f"unseen {c.unknown.p:.0%}")
                    lines.append(f"  ↳ dominant interaction friction: {', '.join(parts)}")
            except Exception:
                pass

    q_lines: list[str] = []
    try:
        for q in (system.questions() or [])[-2:]:
            measurement = q.get("suggested_measurement")
            if measurement:
                q_lines.append(f"- open question: {measurement}")
    except Exception as e:
        logger.debug("Candor questions read failed: %s", e)

    if not lines and not q_lines:
        return None
    text = "\n".join(["", _HEADER, *lines, *q_lines])
    return text[:_CHAR_CAP]


def collect_degraded_tools(system) -> list[dict]:
    """Tools whose calibrated tool_ok reliability sits below the degraded
    threshold with enough observations to mean it (adaptive producer feed,
    plan 4d). Runs on the bridge executor — do not call directly."""
    out: list[dict] = []
    try:
        rows = list(
            system.index.query(
                "SELECT id, pred, args_json FROM facts "
                "WHERE valid_to IS NULL AND pred = 'tool_ok' AND stmt_type IN ('frequency', 'crisp')"
            )
        )
    except Exception as e:
        logger.debug("Candor degraded-tools query failed: %s", e)
        return out
    for r in rows:
        try:
            args = json.loads(r["args_json"])
            if not args or args[0] == "*":
                continue
            n = _observation_count(system, r["id"])
            if n < _MIN_OBSERVATIONS:
                continue
            p = system.predict({"pred": "tool_ok", "args": args}, budget=_PREDICT_BUDGET)
            if p.p < _DEGRADED_P:
                out.append({"tool": str(args[0]), "p": round(p.p, 3), "n": n})
        except Exception:
            continue
    return out


def describe_prediction(system, pred: str, args: list) -> dict | None:
    """Structured prediction for the agent-facing tool. None = no admitted fact."""
    stmt = {"pred": pred, "args": args}
    fid = system.fact_id_for(stmt)
    if fid is None:
        return None
    n = _observation_count(system, fid)
    p = system.predict(stmt, budget=1000)
    out: dict = {"pred": pred, "args": args, "observations": n, "snapshot_id": p.snapshot_id}
    if hasattr(p, "values"):  # CategoricalPrediction
        # raw_counts only tracks binary channels; the categorical prediction
        # carries its own total.
        out["observations"] = p.total_observations
        out["values"] = {name: round(slice_.p, 3) for name, slice_ in p.values.items()}
        out["unknown"] = round(p.unknown.p, 3)
        out["caveats"] = sorted(p.caveats)
    else:
        out["p"] = round(p.p, 3)
        out["ci"] = [round(p.ci[0], 3), round(p.ci[1], 3)]
        out["caveats"] = sorted(p.caveats)
        if out["caveats"]:
            try:
                dist = system.distribution(stmt)
                modes = dist.get("modes") or {}
                # Per-context true rates — the actionable half of a caveat.
                out["context_modes"] = {
                    key: {val: round(g["p"], 3) for val, g in groups.items() if isinstance(g, dict) and "p" in g}
                    for key, groups in list(modes.items())[:4]
                }
            except Exception:
                pass
    return out
