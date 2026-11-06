"""Pernix — lessons applied at the moment of need.

Until 2026-11-06 lessons were effectively write-only. 1,683 sat in memory,
but the only reader that reached the agent was the first-turn scout, keyed
on the task text. On five real repeat failures (dig, rdap x2, jq, a 402)
that key found the right lesson once, and weakly; keyed on the error text,
it ranked first four times out of four. The same failures came back within
hours of their lesson being saved.

This module puts lessons where the need is:

  * error_hint — when a tool fails, look up a lesson by the error's
    distinctive token (the missing command, the content type, the host) and
    append one `[harness hint] past lesson` to the tool result. Appending to
    the tool result leaves the prompt prefix and the tool list untouched.
  * the capability map — facts about this environment read straight from
    tool results (command not installed, host blocked or paywalled, a tool
    that cannot read a content type). Deterministic; entries expire after
    14 days unless seen again, and a success removes them at once.
  * pitfalls_block — up to three matching lessons verbatim plus the map,
    rendered by the harness into every scout report (LLM, fallback, worker)
    so the scout cannot paraphrase them away.
  * a usage ledger (lesson_events) — shown / recurred, so a lesson that is
    shown and does not help can be told apart from one that works.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time

from config import settings

logger = logging.getLogger("pernix.lessons")

_DAY = 86400
_MAP_TTL = 14 * _DAY

# ── distinctive tokens ──────────────────────────────────────────────────
# Generic words never make a match on their own: "command not found" is in
# every missing-command error, and matching on it pulled the jq lesson for a
# dig failure in the replay.
_CNF_RE = re.compile(r"(?:^|[\s:'\"`])([A-Za-z][\w.+-]{1,40}): (?:command )?not found", re.MULTILINE)
_NOT_INSTALLED_RE = re.compile(r"`?([A-Za-z][\w.+-]{1,40})`?\s+(?:is\s+)?(?:not installed|isn't installed|unavailable)")
_NO_MODULE_RE = re.compile(r"No module named '([\w.]+)'")
_CTYPE_RE = re.compile(r"\b((?:application|text|image|audio|video)/[\w.+-]+)")
_URL_HOST_RE = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_BARE_HOST_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|org|net|io|gov|edu|co|info|dev|ai|us|uk))\b")
_STATUS_RE = re.compile(r"\b(?:HTTP\s*|status\s*|returned\s*)(4\d\d|5\d\d)\b", re.IGNORECASE)
_GENERIC = frozenset(
    {
        "bash", "sh", "line", "error", "exit", "command", "not", "found", "failed", "http", "https", "www",
        "the", "text", "application/json", "text/html", "text/plain", "localhost",
        # httpx appends "For more information check: https://developer.mozilla.org/…"
        "developer.mozilla.org",
    }
)  # fmt: skip


def error_tokens(text: str) -> set[str]:
    """Narrow tokens that identify a failure: what is missing, which content
    type was refused, which host blocked. Empty when the error is generic."""
    text = text or ""
    head = text[:2000]
    out: set[str] = set()
    for rx in (_CNF_RE, _NOT_INSTALLED_RE, _NO_MODULE_RE):
        out.update(m.group(1).lower() for m in rx.finditer(head))
    out.update(m.group(1).lower() for m in _CTYPE_RE.finditer(head))
    if _STATUS_RE.search(head) or "blocked" in head.lower() or _BLOCK_PAGE_RE.search(head[:400]):
        out.update(_host_of(m.group(1)) for m in _URL_HOST_RE.finditer(head))
    return {t for t in out if t and t not in _GENERIC}


def lesson_tokens(text: str) -> set[str]:
    """Tokens a lesson's text could match an error on: commands it names,
    content types, hosts."""
    text = text or ""
    low = text.lower()
    out: set[str] = set()
    for rx in (_CNF_RE, _NOT_INSTALLED_RE, _NO_MODULE_RE):
        out.update(m.group(1).lower() for m in rx.finditer(text))
    out.update(m.group(1).lower() for m in re.finditer(r"`([A-Za-z][\w.+-]{1,30})`", text))
    out.update(m.group(1).lower() for m in _CTYPE_RE.finditer(low))
    out.update(_host_of(m.group(1)) for m in _URL_HOST_RE.finditer(low))
    out.update(_host_of(m.group(1)) for m in _BARE_HOST_RE.finditer(low))
    return {t for t in out if t and t not in _GENERIC}


def _host_of(host: str) -> str:
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _mentions(text: str, token: str) -> bool:
    return re.search(r"(?<![\w.-])" + re.escape(token) + r"(?![\w-])", (text or "").lower()) is not None


# ── tables (memory index DB, created on demand) ─────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lesson_sig (
    file_name TEXT NOT NULL, epoch TEXT NOT NULL, token TEXT NOT NULL,
    PRIMARY KEY (file_name, epoch, token)
);
CREATE INDEX IF NOT EXISTS idx_lesson_sig_token ON lesson_sig(token);
CREATE TABLE IF NOT EXISTS capability_facts (
    kind TEXT NOT NULL, key TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS lesson_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, session_id TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL, epoch TEXT NOT NULL, channel TEXT NOT NULL, event TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lesson_events_entry ON lesson_events(file_name, epoch);
"""
_schema_ready: set[str] = set()


def _conn():
    from db.database import connect_memory

    conn = connect_memory()
    if settings.memory_dir not in _schema_ready:
        conn.executescript(_SCHEMA)
        _schema_ready.add(settings.memory_dir)
    return conn


def index_lesson(file_name: str, epoch: int, content: str) -> None:
    """Record a lesson's match tokens (called by store.add_entry). Never raises."""
    try:
        toks = lesson_tokens(content)
        if not toks:
            return
        conn = _conn()
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO lesson_sig (file_name, epoch, token) VALUES (?, ?, ?)",
                [(file_name, str(epoch), t) for t in toks],
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("lesson_sig index failed: %s", e)


def backfill_signatures(store) -> int:
    """One-time: index every live lesson already in the store."""
    conn = store._connect()
    try:
        rows = conn.execute("SELECT file_name, epoch, content FROM memory_fts WHERE entry_type = 'lesson'").fetchall()
    finally:
        conn.close()
    for r in rows:
        index_lesson(r[0], int(r[1]), r[2])
    return len(rows)


def _log_event(session_id: str, file_name: str, epoch: int, channel: str, event: str) -> None:
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO lesson_events (at, session_id, file_name, epoch, channel, event) VALUES (?,?,?,?,?,?)",
                (int(time.time()), session_id or "", file_name, str(epoch), channel, event),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("lesson event log failed: %s", e)


# ── write-side filter ───────────────────────────────────────────────────

_UNSAFE_RE = re.compile(
    r"\b(?:sudo|as root|root shell|privileged shell|chmod\s+(?:-R\s+)?777|verify\s*=\s*False|"
    r"--insecure|--no-check-certificate|disable (?:the )?(?:ssl|tls|sandbox|firewall))\b",
    re.IGNORECASE,
)
_TASK_LOCAL_RE = re.compile(
    r"\b(?:this charter|the charter|test charter|this task's|for this task only)\b", re.IGNORECASE
)


def rejection(content: str) -> str:
    """Why a lesson must not be stored ("" = fine). Applies to every writer."""
    if _UNSAFE_RE.search(content or ""):
        return "unsafe workaround (privilege or security bypass)"
    if _TASK_LOCAL_RE.search(content or ""):
        return "task-local, not reusable"
    return ""


# ── error-time hint ─────────────────────────────────────────────────────


_WEB_TOOLS = frozenset({"http_get", "browse_web", "fetch_url", "crawl4ai_fetch"})
# A block page a web tool returns as a "success" (b70b43b415cb: browse_web
# got news-example.org's "Access Restricted" page with was_error=false).
_BLOCK_PAGE_RE = re.compile(
    r"\b(?:Access (?:Restricted|Denied)|Attention Required|Just a moment|unable to allow access|"
    r"Payment Required|verify you are (?:a )?human|Request blocked)\b",
    re.IGNORECASE,
)


def is_failure(content: str, *, was_error: bool, command_failed: bool, tool: str = "") -> bool:
    """The hint trigger. http_get rejections are stored was_error=False and
    begin "Error …", and web tools return block pages as successes, so the
    text checks are needed alongside the flags."""
    text = (content or "").lstrip()
    if was_error or command_failed or text.startswith("Error"):
        return True
    return tool in _WEB_TOOLS and bool(_BLOCK_PAGE_RE.search(text[:400]))


def _age_days(epoch: int) -> int:
    return max(0, (int(time.time()) - int(epoch or 0)) // _DAY)


def error_hint(tool: str, content: str, fired: set[str], *, session_id: str = "") -> str:
    """One past-lesson hint for this failed tool result, or "".

    A lesson qualifies only when it names one of the error's distinctive
    tokens (sidecar first, then BM25 over lesson rows with the same gate).
    At most one hint per result and each lesson once per turn (`fired`)."""
    if not settings.lesson_hints_enabled:
        return ""
    toks = error_tokens(content)
    if not toks:
        return ""
    try:
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if not store:
            return ""
        candidates: list[tuple[str, int, str]] = []
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT s.file_name, s.epoch FROM lesson_sig s WHERE s.token IN (%s)"
                % ",".join("?" * len(toks)),
                sorted(toks),
            ).fetchall()
        finally:
            conn.close()
        for fn, ep in rows:
            e = store.get_entry(fn, int(ep))
            if e is not None and e.entry_type == "lesson":
                candidates.append((fn, int(ep), e.content))
        if not candidates:
            query = f"{tool} " + " ".join(sorted(toks)) + " " + (content or "")[:300]
            for r in store.search_lessons(query, limit=5, _track_hits=False, lexical_only=True):
                if r.score >= 1.0 and any(_mentions(r.entry.content, t) for t in toks):
                    candidates.append((r.entry.file_name, r.entry.epoch, r.entry.content))
        # Newest first: an older lesson on the same token is likelier stale.
        candidates.sort(key=lambda c: c[1], reverse=True)
        for fn, ep, text in candidates:
            key = f"lesson:{fn}@{ep}"
            if key in fired:
                continue
            fired.add(key)
            recurred = _shown_before(session_id, fn, ep)
            _log_event(session_id, fn, ep, "error_hint", "recurred" if recurred else "shown")
            logger.info("lesson.hint tool=%s lesson=%s@%s tokens=%s", tool, fn, ep, ",".join(sorted(toks)))
            snippet = re.sub(r"\s+", " ", text).strip()[:300]
            return f"[harness hint] past lesson ({_age_days(ep)}d old) on this error: {snippet}"
    except Exception as e:
        logger.debug("error_hint failed: %s", e)
    return ""


def _shown_before(session_id: str, file_name: str, epoch: int) -> bool:
    if not session_id:
        return False
    try:
        conn = _conn()
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM lesson_events WHERE session_id = ? AND file_name = ? AND epoch = ? LIMIT 1",
                    (session_id, file_name, str(epoch)),
                ).fetchone()
                is not None
            )
        finally:
            conn.close()
    except Exception:
        return False


# ── capability map ──────────────────────────────────────────────────────

_PREFIXES = frozenset({"env", "sudo", "nohup", "time", "timeout", "nice", "stdbuf", "command", "exec"})
_PROBES = frozenset({"which", "type", "hash", "whereis"})
_SEG_SPLIT = re.compile(r"\|\||&&|[|;\n]")


def _leading_commands(command: str) -> list[str]:
    """First real command of each pipeline segment, after env/sudo/time
    prefixes and VAR=value assignments."""
    out = []
    for seg in _SEG_SPLIT.split(command or ""):
        try:
            words = shlex.split(seg, posix=True)
        except ValueError:
            words = seg.split()
        while words and (
            "=" in words[0]
            and not words[0].startswith("=")
            or words[0] in _PREFIXES
            or re.fullmatch(r"-\w+|\d+[smh]?", words[0])
        ):
            words = words[1:]
        if words:
            out.append(words[0].rsplit("/", 1)[-1])
    return out


def _upsert(kind: str, key: str, detail: str) -> None:
    now = int(time.time())
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO capability_facts (kind, key, detail, first_seen, last_seen, count) VALUES (?,?,?,?,?,1) "
            "ON CONFLICT(kind, key) DO UPDATE SET last_seen = ?, count = count + 1, detail = ?",
            (kind, key, detail, now, now, now, detail),
        )
        conn.commit()
    finally:
        conn.close()


def _clear(kind: str, key: str) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM capability_facts WHERE kind = ? AND key = ?", (kind, key))
        conn.commit()
    finally:
        conn.close()


def record_outcome(tool: str, args: dict | None, content: str, *, failed: bool) -> None:
    """Feed the capability map from one tool result. Never raises."""
    if not settings.capability_map_enabled:
        return
    try:
        args = args or {}
        text = content or ""
        if tool == "bash":
            leading = _leading_commands(str(args.get("command", "")))
            if failed:
                for m in _CNF_RE.finditer(text[:2000]):
                    cmd = m.group(1).lower()
                    if cmd in leading and cmd not in _GENERIC:
                        _upsert("missing_command", cmd, "not installed")
            elif leading and not (set(leading) & _PROBES):
                for cmd in leading:
                    _clear("missing_command", cmd.lower())
            return
        url = str(args.get("url", "") or "")
        hm = _URL_HOST_RE.search(url)
        if not hm:
            return
        host = _host_of(hm.group(1))
        if failed:
            st = _STATUS_RE.search(text[:600])
            ct = _CTYPE_RE.search(text[:600])
            if ct and "not text" in text[:600]:
                _upsert("tool_limit", f"{tool}:{ct.group(1).lower()}", f"{tool} cannot read {ct.group(1).lower()}")
            elif st and st.group(1) in ("401", "402", "403", "429", "451"):
                _upsert("blocked_host", host, f"HTTP {st.group(1)}")
            elif _BLOCK_PAGE_RE.search(text[:600]) or re.search(r"captcha|bot detect", text[:600], re.IGNORECASE):
                _upsert("blocked_host", host, "bot wall")
        elif len(text) > 500 and not text.lstrip().startswith("Error"):
            _clear("blocked_host", host)
    except Exception as e:
        logger.debug("capability record failed: %s", e)


def capability_line(max_chars: int = 220) -> str:
    """One line of live environment facts, or "" when there are none."""
    if not settings.capability_map_enabled:
        return ""
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT kind, key, detail FROM capability_facts WHERE last_seen >= ? ORDER BY last_seen DESC",
                (int(time.time()) - _MAP_TTL,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return ""
    if not rows:
        return ""
    missing = [k for kind, k, _ in rows if kind == "missing_command"][:8]
    blocked = [f"{k} ({d})" for kind, k, d in rows if kind == "blocked_host"][:5]
    limits = [d for kind, _, d in rows if kind == "tool_limit"][:3]
    parts = []
    if missing:
        parts.append("not installed: " + ", ".join(missing))
    if blocked:
        parts.append("blocked: " + ", ".join(blocked))
    if limits:
        parts.append("; ".join(limits))
    line = "Environment (from recent tool results): " + "; ".join(parts)
    return line[:max_chars]


# ── known-pitfalls block ────────────────────────────────────────────────


def pitfalls_block(message: str, tools: list[str] | None = None, *, session_id: str = "", max_chars: int = 600) -> str:
    """Harness-filled [KNOWN PITFALLS] block for a scout report, or "".

    Up to three lessons quoted verbatim (score >= 1.0 after age decay) plus
    the capability line. The query adds the planned tool names to the task
    text: the rdap miss was on task text, while the plan named http_get."""
    if not settings.lesson_pitfalls_enabled:
        return ""
    lines: list[str] = []
    try:
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if store and (message or "").strip():
            query = (message or "")[:600] + " " + " ".join((tools or [])[:10])
            for r in store.search_lessons(query, limit=3, _track_hits=False):
                if r.score < 1.0:  # the documented weak floor; task text is a long query
                    continue
                lines.append("- " + re.sub(r"\s+", " ", r.entry.content).strip()[:220])
                _log_event(session_id, r.entry.file_name, r.entry.epoch, "pitfalls", "shown")
    except Exception as e:
        logger.debug("pitfalls lessons failed: %s", e)
    cap = capability_line()
    if cap:
        lines.append("- " + cap)
    if not lines:
        return ""
    block = "[KNOWN PITFALLS — from past tool failures; check before you act]\n" + "\n".join(lines)
    return block[:max_chars]


def usage_stats(days: int = 7) -> dict:
    """Shown / recurred counts per channel over the window."""
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT channel, event, COUNT(*) FROM lesson_events WHERE at >= ? GROUP BY channel, event",
                (int(time.time()) - days * _DAY,),
            ).fetchall()
        finally:
            conn.close()
        return {f"{c}.{e}": n for c, e, n in rows}
    except Exception:
        return {}


def dumps(obj) -> str:
    return json.dumps(obj)
