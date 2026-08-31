"""Pernix — Session tools extension: cross-session introspection."""

from __future__ import annotations

from db import models as db


def list_recent_sessions(limit: int = 10, _context: dict | None = None) -> str:
    """List recent sessions with titles, types, message counts, and timestamps."""
    sessions = db.list_sessions_enriched(limit=limit)
    if not sessions:
        return "No sessions found."
    lines = []
    for s in sessions:
        msg_count = s.get("message_count", 0)
        tokens = s.get("total_tokens", 0)
        tokens_str = f"  {tokens:,} tokens" if tokens else ""
        parent = f"  parent={s['parent_session_id'][:8]}" if s.get("parent_session_id") else ""
        first = s.get("first_message") or ""
        first_str = f'  "{first[:80]}"' if first else ""
        lines.append(
            f"- {s['id'][:8]} [{s['session_type']}] \"{s['title']}\""
            f"  {(s.get('created_at') or '')[:16]} → {(s.get('updated_at') or '')[:16]}"
            f"  {msg_count} msgs{tokens_str}{parent}{first_str}"
        )
    return "\n".join(lines)


def read_session_summary(session_id: str, recent: int = 5, _context: dict | None = None) -> str:
    """Read a rich summary of a session: metadata, token usage, and recent conversation."""
    session = db.get_session(session_id)
    if not session:
        # Try prefix match — caller may pass first 8 chars
        all_sessions = db.list_sessions(limit=200)
        matches = [s for s in all_sessions if s["id"].startswith(session_id)]
        if len(matches) == 1:
            session = matches[0]
            session_id = session["id"]
        elif len(matches) > 1:
            ids = ", ".join(s["id"][:8] for s in matches)
            return f"Ambiguous prefix '{session_id}' matches: {ids}"
        else:
            return f"Session '{session_id}' not found."

    usage = db.get_session_usage(session_id)
    messages = db.get_messages(session_id)

    # --- Metadata block ---
    created = (session.get("created_at") or "")[:19]
    updated = (session.get("updated_at") or "")[:19]
    parent = session.get("parent_session_id") or ""
    subtitle = session.get("subtitle") or ""
    state = session.get("state_v2") or session.get("state") or "unknown"

    lines = [
        f"Session: {session['id'][:8]}  \"{session['title']}\"",
    ]
    if subtitle:
        lines.append(f"Subtitle: {subtitle}")
    lines += [
        f"Type: {session['session_type']}  |  State: {state}",
        f"Started: {created}  |  Last active: {updated}",
    ]
    if parent:
        lines.append(f"Parent session: {parent[:8]}")

    # Token usage
    total_tok = usage.get("total", 0)
    cost = usage.get("cost", 0.0)
    calls = usage.get("calls", 0)
    if total_tok:
        lines.append(
            f"Tokens: {total_tok:,} total  ({usage.get('prompt',0):,} prompt / "
            f"{usage.get('completion',0):,} completion / "
            f"{usage.get('cache_read',0):,} cache_read)  "
            f"Est. cost: ${cost:.4f}  LLM calls: {calls}"
        )

    # Message counts
    user_msgs = [m for m in messages if m["role"] == "user"]
    asst_msgs = [m for m in messages if m["role"] == "assistant"]
    lines.append(f"Messages: {len(user_msgs)} user / {len(asst_msgs)} assistant / {len(messages)} total")

    # First user message — establishes what the session was about
    if user_msgs:
        first_content = (user_msgs[0].get("content") or "").strip()[:300]
        lines += ["", "First message:", f"  [user] {first_content}"]

    # Recent conversation turns
    recent_msgs = [m for m in messages[-recent * 4 :] if m["role"] in ("user", "assistant")][-recent:]
    if recent_msgs:
        lines.append(f"\nRecent {len(recent_msgs)} messages:")
        for m in recent_msgs:
            content = (m.get("content") or "").strip()[:400]
            lines.append(f"  [{m['role']}] {content}")

    return "\n".join(lines)


def agent_state(_context: dict | None = None) -> str:
    """One-call digest of platform state around the agent — the deep,
    on-demand companion to the ambient [SINCE YOUR LAST TURN] ledger
    (agent-ergonomics plan §4.2/§2.7: 'one observability query' instead of
    6-10 per self-inspection turn). Composes existing tables; every section
    is guarded so a missing feature yields a shorter digest, not an error."""
    from datetime import datetime, timezone

    sid = (_context or {}).get("session_id") or ""
    lines = [f"AGENT STATE — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"]

    busy_states = ("scouting", "processing", "compacting", "awaiting_workers")
    with db.connect_sessions() as conn:
        try:
            marks = ",".join("?" for _ in busy_states)
            rows = conn.execute(
                f"SELECT session_type, COUNT(*) c FROM sessions WHERE state_v2 IN ({marks}) GROUP BY session_type",
                busy_states,
            ).fetchall()
            busy = {r["session_type"]: r["c"] for r in rows}
            jobs = conn.execute("SELECT COUNT(*) c FROM jobs WHERE state = 'running'").fetchone()["c"]
            rlm = conn.execute("SELECT COUNT(*) c FROM rlm_runs WHERE status = 'running'").fetchone()["c"]
            flight = ", ".join(f"{v} {k}" for k, v in sorted(busy.items())) or "none"
            lines.append(f"IN FLIGHT: sessions busy: {flight}; jobs running: {jobs}; RLM runs: {rlm}")
        except Exception:
            pass
        if sid:
            try:
                pms = conn.execute(
                    "SELECT verdict, failure_cause, created_at FROM post_mortems "
                    "WHERE session_id = ? ORDER BY created_at DESC LIMIT 3",
                    (sid,),
                ).fetchall()
                if pms:
                    verdicts = ", ".join(f"{p['verdict']}({p['failure_cause']}) {p['created_at'][:16]}" for p in pms)
                    lines.append(f"THIS SESSION'S RECENT VERDICTS (grader's opinion): {verdicts}")
            except Exception:
                pass
        try:
            notes = conn.execute(
                "SELECT title, urgency, created_at FROM notifications ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            if notes:
                lines.append("RECENT NOTIFICATIONS:")
                for n in notes:
                    lines.append(f"  - [{n['urgency']}] {n['title']} ({(n['created_at'] or '')[:16]})")
        except Exception:
            pass
        try:
            kinds = conn.execute(
                "SELECT kind, COUNT(*) c FROM adaptive_entries WHERE status = 'active' GROUP BY kind"
            ).fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) c, SUM(CASE WHEN producer = 'agent' THEN 1 ELSE 0 END) mine "
                "FROM adaptive_proposals WHERE status = 'pending'"
            ).fetchone()
            # status is the authoritative field — flagged_reason survives a
            # clear/expiry, and counting it over-reported 5 where 1 batch was
            # actually suspect (found by the agent live-validating this tool).
            suspect = conn.execute("SELECT COUNT(*) c FROM adaptive_batches WHERE status = 'suspect'").fetchone()["c"]
            if kinds or (pending and pending["c"]):
                kind_str = ", ".join(f"{k['c']} {k['kind']}" for k in kinds) or "none"
                lines.append(
                    f"ADAPTIVE: active entries: {kind_str}; pending proposals: "
                    f"{pending['c'] if pending else 0} ({int(pending['mine'] or 0) if pending else 0} yours); "
                    f"suspect batches: {suspect}"
                )
        except Exception:
            pass
        try:
            fails = conn.execute(
                "SELECT task, created_at FROM canary_runs WHERE outcome = 'gate_fail' "
                "ORDER BY created_at DESC LIMIT 3"
            ).fetchall()
            if fails:
                lines.append(
                    "CANARY RECENT GATE-FAILS: "
                    + "; ".join(f"{f['task']} ({(f['created_at'] or '')[:16]})" for f in fails)
                )
        except Exception:
            pass
        try:
            crons = conn.execute(
                "SELECT COUNT(*) c, SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) bad "
                "FROM cron_runs WHERE started_at > datetime('now', '-3 days')"
            ).fetchone()
            if crons and crons["c"]:
                lines.append(f"CRON (3d): {crons['c']} runs, {int(crons['bad'] or 0)} non-ok")
        except Exception:
            pass

    try:
        from pathlib import Path as _P

        from config import settings as _s

        mem_files = len(list(_P(_s.memory_dir).glob("*.md"))) if getattr(_s, "memory_dir", "") else 0
        if mem_files:
            lines.append(f"MEMORY STORE: {mem_files} files (recall/deep_recall to query)")
    except Exception:
        pass
    try:
        from config import settings as _s

        if _s.telos_enabled:
            from core.telos.store import TelosStore

            store = TelosStore.open()
            alarms = store.list_alarms(open_only=True)
            qs = store.list_questions(state="open")
            if alarms or qs:
                lines.append(f"TELOS: {len(qs)} open questions, {len(alarms)} alarms (telos_status for detail)")
    except Exception:
        pass

    if len(lines) == 1:
        lines.append("(all quiet — nothing in flight, no recent verdicts or notifications)")
    lines.append("Deeper: SYSTEM-MAP.md in the workspace maps every table, route, and store.")
    return "\n".join(lines)


def register(reg) -> None:
    common = {"category": "session", "source": "extension"}
    tags = ["session", "history", "recent", "previous", "past"]

    reg.register(
        name="list_recent_sessions",
        func=list_recent_sessions,
        description=(
            "List recent sessions with titles, types, timestamps, message counts, "
            "token usage, and the opening message of each session."
        ),
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max results (default 10)"}},
        },
        tags=tags + ["list", "all"],
        timeout=15,
        parallel_safe=True,
        **common,
    )
    reg.register(
        name="agent_state",
        func=agent_state,
        description=(
            "One-call digest of platform state: work in flight (sessions, jobs, RLM), "
            "this session's recent reflect verdicts, recent notifications, adaptive-layer "
            "state (active entries, pending proposals, suspect batches), recent canary "
            "gate-fails, cron health, memory-store size, telos alarms. Use INSTEAD of "
            "querying each subsystem separately when asked about Pernix's own state."
        ),
        parameters={"type": "object", "properties": {}},
        tags=["state", "status", "self", "platform", "health", "introspection", "observability"],
        timeout=15,
        parallel_safe=True,
        **common,
    )
    reg.register(
        name="read_session_summary",
        func=read_session_summary,
        description=(
            "Read a rich summary of a specific session: metadata (type, state, "
            "start/end times, parent), token usage, message counts, the opening "
            "message, and the most recent conversation turns. "
            "Accepts full session ID or an 8-char prefix."
        ),
        parameters={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID or 8-char prefix to look up",
                },
                "recent": {
                    "type": "integer",
                    "description": "Number of recent user/assistant messages to include (default 5)",
                },
            },
            "required": ["session_id"],
        },
        tags=tags + ["read", "summary", "detail", "metadata", "tokens", "cost"],
        timeout=30,
        parallel_safe=True,
        **common,
    )
