import json

"""Pernix — Database query helpers organized by table."""

import logging
import re
import sqlite3
import uuid
from bisect import bisect_right
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from db.database import connect_sessions

logger = logging.getLogger("pernix.db")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(length: int = 12) -> str:
    return uuid.uuid4().hex[:length]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(
    title: str = "New session",
    system_prompt: str = "",
    session_type: str = "normal",
    parent_session_id: str | None = None,
    space_id: str | None = None,
) -> str:
    """Create a new session. Returns session ID."""
    sid = _new_id()
    now = _now()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO sessions (id, title, system_prompt, session_type,
               parent_session_id, space_id, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'idle', ?, ?)""",
            (sid, title, system_prompt, session_type, parent_session_id, space_id, now, now),
        )
    return sid


def get_session(session_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None


def list_sessions(limit: int = 50, offset: int = 0) -> list[dict]:
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


# The session types the sidebar's legend names, and the only values
# `exclude_types` accepts. They are the `session_type` column's own words, not
# a title heuristic: the old `title LIKE 'Cron: %'` sweep deleted a chat
# somebody had renamed and never saw a cron session titled anything else
# (see list_cron_sessions_before), and a filter that hides the wrong rows is
# worse than one that hides none.
SESSION_TYPE_NAMES = ("normal", "worker", "cron", "rlm", "snooze", "canary")


def _exclude_types_sql(exclude_types: Iterable[str] | None, prefix: str = "") -> tuple[str, list[str]]:
    """A WHERE condition that leaves whole session types out, and its params.

    Unknown names are dropped rather than rejected, so a client that has
    learned a type this build never heard of degrades to showing it instead
    of erroring on every list request.

    COALESCE is what makes 'normal' mean what a reader expects. The column
    has defaulted to 'normal' for a long time, but rows written before that
    default hold NULL, and an untyped session IS an ordinary chat — without
    the COALESCE, excluding 'normal' would leave exactly the oldest chats on
    screen.
    """
    names = [t for t in dict.fromkeys(exclude_types or ()) if t in SESSION_TYPE_NAMES]
    if not names:
        return "", []
    holes = ",".join("?" * len(names))
    return f"COALESCE({prefix}session_type, 'normal') NOT IN ({holes})", names


def count_sessions(*, archived: bool | None = False, exclude_types: Iterable[str] | None = None) -> int:
    """How many sessions exist — what the sidebar's page is a slice of.
    Without it the list could only say "showing the most recent N" and leave
    the reader to guess whether anything was behind it.

    ``archived`` selects which population is being counted: False (default)
    the live ones, True the archived ones, None every row. The default is
    False rather than None because every caller counting "the sessions"
    means the ones the list is showing, and an archived session is exactly
    the one that has left it.

    ``exclude_types`` narrows it the same way the listing is narrowed, so
    ``total`` and ``has_more`` count the population the caller is actually
    being shown rather than one it has asked the server to leave out.
    """
    conds: list[str] = []
    if archived is True:
        conds.append("archived_at IS NOT NULL")
    elif archived is False:
        conds.append("archived_at IS NULL")
    ex_sql, params = _exclude_types_sql(exclude_types)
    if ex_sql:
        conds.append(ex_sql)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with connect_sessions() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM sessions{where}", params).fetchone()
        return int(row["c"]) if row else 0


_ENRICHED_SELECT = """SELECT
    s.*,
    COALESCE(mc.message_count, 0) AS message_count,
    COALESCE(tu.total_tokens, 0) AS total_tokens,
    COALESCE(tu.total_cost, 0) AS total_cost,
    fm.first_message
FROM sessions s
LEFT JOIN (
    SELECT session_id, COUNT(*) AS message_count
    FROM messages
    WHERE role IN ('user', 'assistant')
    GROUP BY session_id
) mc ON mc.session_id = s.id
LEFT JOIN (
    SELECT session_id, SUM(total_tokens) AS total_tokens,
           SUM(COALESCE(cost_estimate, 0)) AS total_cost
    FROM token_usage
    GROUP BY session_id
) tu ON tu.session_id = s.id
LEFT JOIN (
    SELECT session_id, substr(content, 1, 200) AS first_message
    FROM (
        SELECT session_id, content,
               ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY id) AS rn
        FROM messages
        WHERE role = 'user'
    )
    WHERE rn = 1
) fm ON fm.session_id = s.id
"""


def list_sessions_enriched(
    limit: int = 50,
    offset: int = 0,
    *,
    archived: bool = False,
    exclude_types: Iterable[str] | None = None,
) -> list[dict]:
    """List sessions with message_count, total_tokens, and first_message.

    Space sessions are long-lived by contract: any that fall outside the
    recency window are unioned back in, so the sidebar's space groups never
    lose members to the LIMIT no matter how stale they get.

    ``archived`` picks the population: False (default) the live list, True
    the archived one. Archiving is precisely how a session leaves the
    sidebar and its space group, so the never-roll-off union is skipped for
    the archived page — an archived space session belongs in the Archived
    group, not back in the space it was pulled out of.

    ``exclude_types`` drops whole session types in SQL, BEFORE the LIMIT.
    That is the whole point of it: a box whose 500 newest rows are 277 canary
    self-checks was spending more than half of every page on sessions the
    user had already told the legend to hide, and the chats they were looking
    for sat behind a "load older" button. Filtering after the page has been
    cut only makes the page shorter. The space union takes the same clause,
    or an excluded type would walk straight back in through it.
    """
    conds = ["s.archived_at IS NOT NULL" if archived else "s.archived_at IS NULL"]
    ex_sql, ex_params = _exclude_types_sql(exclude_types, "s.")
    if ex_sql:
        conds.append(ex_sql)
    where = "WHERE " + " AND ".join(conds) + " "
    with connect_sessions() as conn:
        rows = conn.execute(
            _ENRICHED_SELECT + where + "ORDER BY s.updated_at DESC LIMIT ? OFFSET ?",
            (*ex_params, limit, offset),
        ).fetchall()
        result = [dict(r) for r in rows]
        if archived:
            return result
        # _ENRICHED_SELECT GROUP BYs all of messages and token_usage and runs
        # a ROW_NUMBER() over every user message before the outer LIMIT, so
        # running it twice doubles the disk and CPU of every sidebar refresh.
        # With no spaces configured the second pass can only return rows the
        # first already has.
        if not conn.execute("SELECT 1 FROM spaces LIMIT 1").fetchone():
            return result
        seen = {r["id"] for r in result}
        union_where = "WHERE s.space_id IS NOT NULL AND s.archived_at IS NULL "
        if ex_sql:
            union_where += "AND " + ex_sql + " "
        extra = conn.execute(
            _ENRICHED_SELECT + union_where + "ORDER BY s.updated_at DESC",
            ex_params,
        ).fetchall()
        result.extend(dict(r) for r in extra if r["id"] not in seen)
        return result


def count_sessions_by_type() -> dict[str, int]:
    """How many live sessions there are of each type — the legend's numbers.

    Deliberately NOT narrowed by ``exclude_types``: the legend has to keep
    naming what it is hiding. A count that fell to zero the moment a type was
    filtered out would erase the only control that turns it back on, and the
    user would be left with a dot they could no longer read.

    Archived sessions are out for the same reason they are out of the list —
    the legend is a key to what is on screen, and the archive has its own
    entry with its own count. One GROUP BY over ``sessions``, which is the
    same shape and cost as the COUNT(*) beside it.

    The six known names are always present, at 0 if nothing wears them. A
    type this build does not know about is reported under its own name
    rather than folded into 'normal': it is a real population, and calling
    it an ordinary chat would be a lie the client cannot check.
    """
    out = dict.fromkeys(SESSION_TYPE_NAMES, 0)
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT COALESCE(session_type, 'normal') AS t, COUNT(*) AS c "
            "FROM sessions WHERE archived_at IS NULL GROUP BY t"
        ).fetchall()
    for r in rows:
        out[r["t"]] = out.get(r["t"], 0) + int(r["c"])
    return out


# Everything older than the cutoff, bucketed by the first rule that spares it.
# One statement over the whole table: the purge used to read the 1,000 most
# recently updated rows and filter those, which made the OLDEST sessions — the
# only ones a bulk purge is for — the ones it could not see, and widened the
# blind spot by one row per new session.
#
# The message count is computed inside the CASE so the correlated subquery
# runs only for rows that are actually candidates; SQLite evaluates CASE
# branches lazily, so a table full of pinned and typed sessions costs one
# scan, not one scan plus a COUNT per row.
_PURGE_CANDIDATES_SQL = """SELECT
    s.id,
    s.title,
    COALESCE(s.session_type, 'normal') AS session_type,
    s.updated_at,
    CASE
        WHEN COALESCE(s.session_type, 'normal') != 'normal' THEN 'other_types'
        WHEN COALESCE(s.pinned, 0) != 0 THEN 'pinned'
        WHEN s.space_id IS NOT NULL THEN 'in_space'
        ELSE ''
    END AS spared_by,
    CASE
        WHEN COALESCE(s.session_type, 'normal') = 'normal'
             AND COALESCE(s.pinned, 0) = 0
             AND s.space_id IS NULL
        THEN (SELECT COUNT(*) FROM messages m
              WHERE m.session_id = s.id AND m.role IN ('user', 'assistant'))
        ELSE 0
    END AS message_count
FROM sessions s
WHERE s.updated_at < ?
ORDER BY s.updated_at DESC
"""


def list_purge_candidates(cutoff_iso: str) -> dict:
    """What the bulk purge may delete, and what it deliberately spared.

    A candidate is an ordinary user chat gone stale: ``session_type =
    'normal'``, not pinned, not in a space, ``updated_at`` before the cutoff.
    Everything else older than the cutoff is counted under the FIRST rule
    that spares it — other_types, then pinned, then in_space — so the buckets
    partition the excluded rows and can be shown to a user as a sentence
    rather than three overlapping numbers.

    The three rules exist for three different reasons. Canary, worker, cron,
    rlm and snooze sessions each have their own horizon in core/retention.py,
    tuned to what that machinery needs to keep; a blanket age sweep deleted
    them out from under it. Pinning is the user saying "keep this" and had no
    effect here at all. Space sessions are long-lived by contract (v33) and
    leave only by explicit delete or their space's cascade.

    Returns ``{"candidates": [{id, title, session_type, updated_at,
    message_count}, ...] newest first, "skipped": {"pinned": n, "in_space":
    n, "other_types": n}}``.
    """
    candidates: list[dict] = []
    skipped = {"pinned": 0, "in_space": 0, "other_types": 0}
    with connect_sessions() as conn:
        for r in conn.execute(_PURGE_CANDIDATES_SQL, (cutoff_iso,)):
            spared_by = r["spared_by"]
            if spared_by:
                skipped[spared_by] += 1
                continue
            candidates.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "session_type": r["session_type"],
                    "updated_at": r["updated_at"],
                    "message_count": int(r["message_count"] or 0),
                }
            )
    return {"candidates": candidates, "skipped": skipped}


# ---------------------------------------------------------------------------
# Archive (migration v34)
#
# Both selectors are ONE statement over the whole table. The pruners that
# walked list_sessions(500) could not see the oldest rows — exactly the ones
# an age sweep is for — and an archive sweep has the same shape and would
# have inherited the same blind spot.
# ---------------------------------------------------------------------------


def list_idle_sessions_to_archive(cutoff_iso: str, space_id: str | None = None) -> list[dict]:
    """Ordinary chats idle since before the cutoff and not already archived.

    'normal' only: canary, worker, cron, rlm and snooze sessions are
    machinery with their own horizon in core/retention.py, and archiving
    them would only hide residue that is due to be deleted anyway. Pinned is
    the user saying keep this in front of me, so it is exempt here for the
    same reason it is exempt from the purge.

    Space sessions ARE included. A space is a grouping, not a promise of
    permanent visibility: the v33 rule that keeps them out of every DELETE
    sweep is about never losing the transcript, and archiving loses nothing.

    ``space_id`` narrows the sweep to one space ("Archive idle sessions…"
    on a space header); None sweeps the whole table.
    """
    sql = """SELECT s.id, s.title, s.updated_at, s.space_id
             FROM sessions s
             WHERE COALESCE(s.session_type, 'normal') = 'normal'
               AND s.archived_at IS NULL
               AND COALESCE(s.pinned, 0) = 0
               AND s.updated_at < ?"""
    params: list = [cutoff_iso]
    if space_id:
        sql += " AND s.space_id = ?"
        params.append(space_id)
    sql += " ORDER BY s.updated_at ASC"
    with connect_sessions() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def list_archived_sessions_before(cutoff_iso: str) -> list[dict]:
    """Sessions archived before the cutoff — the hard-delete sweep's input.

    Any session type: once a row carries an archived_at it is in the
    archive, and the archive has one horizon rather than six.
    """
    with connect_sessions() as conn:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT s.id, s.title, s.updated_at, s.space_id, s.archived_at
                   FROM sessions s
                   WHERE s.archived_at IS NOT NULL AND s.archived_at < ?
                   ORDER BY s.archived_at ASC""",
                (cutoff_iso,),
            )
        ]


def update_session(session_id: str, **kwargs) -> None:
    """Update session fields. Only known columns are set."""
    allowed = {
        "title",
        "subtitle",
        "system_prompt",
        "session_type",
        "parent_session_id",
        "state",
        "state_v2",
        "watched_worker_ids",
        "model_override",
        "worker_kind",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [session_id]
    with connect_sessions() as conn:
        conn.execute(f"UPDATE sessions SET {cols} WHERE id = ?", vals)


def touch_session(session_id: str) -> None:
    """Bump only updated_at — for writers (e.g. journal notices) that add
    content outside add_message's chat path, so recency ordering and
    retention windows see the session as live."""
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))


_UNSET = object()


def set_session_meta(
    session_id: str,
    *,
    title: str | None = None,
    pinned: bool | None = None,
    space_id: object = _UNSET,
    archived: bool | None = None,
) -> None:
    """Set user-facing session metadata WITHOUT bumping updated_at —
    renaming, pinning, moving a session between spaces or archiving it must
    not change its recency ordering. space_id accepts None (remove from
    space); omit the argument to leave membership untouched.

    ``archived`` is the same contract one level up: True stamps
    ``archived_at`` with now, False clears it. Recency is what the idle
    horizon and the sidebar's time buckets are computed from, so a restore
    has to put the session back exactly where it was rather than at the top
    of Today."""
    updates: dict = {}
    if title is not None:
        updates["title"] = title
    if pinned is not None:
        updates["pinned"] = 1 if pinned else 0
    if space_id is not _UNSET:
        updates["space_id"] = space_id
    if archived is not None:
        updates["archived_at"] = _now() if archived else None
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [session_id]
    with connect_sessions() as conn:
        conn.execute(f"UPDATE sessions SET {cols} WHERE id = ?", vals)


def set_session_state_v2(session_id: str, state_v2: str) -> None:
    """Persist the v2 state name (one of the 10 SessionStateV2 values) so
    restarts can restore AWAITING_WORKERS, AWAITING_USER, FINALIZING and
    other states the legacy `state` column collapses to "idle"."""
    update_session(session_id, state_v2=state_v2)


def set_watched_workers(session_id: str, worker_ids: list[str]) -> None:
    """Persist the parent's watch-set as a JSON array. Called whenever
    `_watched_worker_ids` mutates so a restart in AWAITING_WORKERS
    doesn't lose the list of workers being awaited."""
    import json as _json

    update_session(session_id, watched_worker_ids=_json.dumps(list(worker_ids)))


# ---------------------------------------------------------------------------
# Spaces — named/colored long-lived session groups (migration v33)
# ---------------------------------------------------------------------------


def create_space(label: str, color: str, slug: str) -> dict:
    """Create a space. Slug uniqueness is enforced by the UNIQUE constraint;
    callers validate the slug format (core.spaces.SLUG_RE) before calling."""
    space_id = _new_id()
    now = _now()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO spaces (id, slug, label, color, sort_order, created_at, updated_at)
               VALUES (?, ?, ?, ?,
                       COALESCE((SELECT MAX(sort_order) + 1 FROM spaces), 0), ?, ?)""",
            (space_id, slug, label, color, now, now),
        )
        row = conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
        return dict(row)


def get_space(space_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
        return dict(row) if row else None


def get_space_by_slug(slug: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM spaces WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None


def list_spaces() -> list[dict]:
    """All spaces with a live session count, in user sort order.

    Live means not archived: an archived session has left its space group
    for the Archived one, so counting it here would name a number the group
    below does not show."""
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT sp.*, COALESCE(sc.session_count, 0) AS session_count
               FROM spaces sp
               LEFT JOIN (
                   SELECT space_id, COUNT(*) AS session_count
                   FROM sessions
                   WHERE space_id IS NOT NULL AND archived_at IS NULL
                   GROUP BY space_id
               ) sc ON sc.space_id = sp.id
               ORDER BY sp.sort_order, sp.created_at""",
        ).fetchall()
        return [dict(r) for r in rows]


def update_space(space_id: str, **kwargs) -> None:
    """Update space fields. Slug is deliberately NOT updatable — memory
    prefixes, directive dirs and workspace homes key off it."""
    allowed = {"label", "color", "sort_order"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [space_id]
    with connect_sessions() as conn:
        conn.execute(f"UPDATE spaces SET {cols} WHERE id = ?", vals)


def delete_space(space_id: str) -> None:
    """Delete the space row only — session detach/cascade is orchestrated
    by the API layer, which owns kernel/memory/workspace cleanup too."""
    with connect_sessions() as conn:
        conn.execute("DELETE FROM spaces WHERE id = ?", (space_id,))


def list_space_session_ids(space_id: str) -> list[str]:
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE space_id = ? ORDER BY created_at",
            (space_id,),
        ).fetchall()
        return [r["id"] for r in rows]


def detach_space_sessions(space_id: str) -> int:
    """Return sessions of a deleted space to the ungrouped list."""
    with connect_sessions() as conn:
        cur = conn.execute("UPDATE sessions SET space_id = NULL WHERE space_id = ?", (space_id,))
        return cur.rowcount


def any_space_session_mid_turn(space_id: str) -> bool:
    """True when ANY session of the space is processing a turn — the shared
    space kernel must not be evicted or reaped while a member is mid-turn.
    Mirrors the single-session check (state_v2 == 'processing') in
    KernelRegistry._session_mid_turn."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE space_id = ? AND state_v2 = 'processing' LIMIT 1",
            (space_id,),
        ).fetchone()
        return row is not None


# ---------------------------------------------------------------------------
# Space suggestions (migration v35) — proposals, never spaces on their own
# ---------------------------------------------------------------------------

# Statuses that end a suggestion's life, and so stamp resolved_at.
SPACE_SUGGESTION_TERMINAL = ("accepted", "rejected", "expired")


def _decode_space_suggestion(row) -> dict:
    """Row → dict with session_ids/directives decoded.

    The two JSON columns are written by this module and read by the API and
    the sidebar, but the table is hand-editable like every other Pernix
    store — a row whose JSON was mangled degrades to "no members / no
    drafts" instead of breaking the whole listing.
    """
    out = dict(row)
    try:
        ids = json.loads(out.get("session_ids_json") or "[]")
    except ValueError:
        ids = []
    out["session_ids"] = [str(i) for i in ids] if isinstance(ids, list) else []
    directives = None
    raw = out.get("directives_json")
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        directives = parsed if isinstance(parsed, dict) else None
    out["directives"] = directives
    return out


def add_space_suggestion(
    kind: str,
    topic_key: str,
    label: str,
    color: str,
    why: str,
    session_ids: list[str],
    *,
    existing_space_id: str | None = None,
    directives: dict | None = None,
) -> dict:
    """Store one pending suggestion. Callers normalize topic_key/label/color
    first — nothing model-supplied reaches a path or a slug from here."""
    suggestion_id = _new_id()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO space_suggestions (id, kind, topic_key, label, color, why,
               existing_space_id, session_ids_json, directives_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                suggestion_id,
                kind,
                topic_key,
                label,
                color,
                why,
                existing_space_id,
                json.dumps(list(session_ids)),
                json.dumps(directives) if directives else None,
                _now(),
            ),
        )
        row = conn.execute("SELECT * FROM space_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return _decode_space_suggestion(row)


def get_space_suggestion(suggestion_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM space_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return _decode_space_suggestion(row) if row else None


def list_space_suggestions(status: str | None = None) -> list[dict]:
    """Newest first. status=None returns every row whatever its state."""
    with connect_sessions() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM space_suggestions WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM space_suggestions ORDER BY created_at DESC").fetchall()
    return [_decode_space_suggestion(r) for r in rows]


def set_space_suggestion_status(suggestion_id: str, status: str, *, space_id: str | None = None) -> None:
    """Advance a suggestion. Terminal statuses stamp resolved_at so the
    declined list can say when the user turned it down."""
    resolved = _now() if status in SPACE_SUGGESTION_TERMINAL else None
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE space_suggestions SET status = ?, resolved_at = ?, space_id = COALESCE(?, space_id) WHERE id = ?",
            (status, resolved, space_id, suggestion_id),
        )


def delete_space_suggestion(suggestion_id: str) -> bool:
    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM space_suggestions WHERE id = ?", (suggestion_id,))
        return cur.rowcount > 0


def delete_space_suggestions_by_status(status: str) -> int:
    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM space_suggestions WHERE status = ?", (status,))
        return cur.rowcount


def expire_space_suggestions(cutoff_iso: str) -> int:
    """Pending rows older than the cutoff become 'expired'. They stay in the
    table: an expired suggestion is history the user can still clear, and
    the scan must not re-offer it as if it were new."""
    with connect_sessions() as conn:
        cur = conn.execute(
            "UPDATE space_suggestions SET status = 'expired', resolved_at = ? "
            "WHERE status = 'pending' AND created_at < ?",
            (_now(), cutoff_iso),
        )
        return cur.rowcount


def count_sessions_created_after(created_after_iso: str) -> int:
    """Ordinary sessions created since a watermark — the "is there anything
    new to look at" test the scheduled scan short-circuits on."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE session_type = 'normal' AND created_at > ?",
            (created_after_iso or "",),
        ).fetchone()
        return int(row["n"]) if row else 0


def list_space_suggest_candidates(cutoff_iso: str) -> list[dict]:
    """Sessions the space-suggestion scan may group, in two queries.

    Two, not one per session: the box carries hundreds of sessions in a
    30-day window and the scan runs on the idle loop, so the cost has to be
    O(rows) rather than O(sessions) round trips. The correlated subqueries
    all ride idx_messages_session.

    Archived sessions are INCLUDED on purpose — a habit that has already
    rolled off the sidebar is exactly the kind worth giving a home. Sessions
    still called 'New session' with no subtitle are skipped: nothing has
    named them yet, so the model would be clustering on nothing. One user
    message is enough otherwise: the quick one-shot asks ("summarize this
    video") are exactly the shape a recurring habit takes, and the box's
    loose YouTube sessions were mostly that shape when a two-message floor
    hid them from the scan.
    """
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT s.id,
                      s.title,
                      COALESCE(s.subtitle, '') AS subtitle,
                      s.space_id,
                      COALESCE(sp.label, '') AS space_label,
                      substr(s.created_at, 1, 10) AS day,
                      (SELECT COUNT(*) FROM messages m
                        WHERE m.session_id = s.id
                          AND m.role IN ('user', 'assistant')
                          AND m.content != '') AS messages,
                      COALESCE((SELECT substr(m.content, 1, 400) FROM messages m
                                 WHERE m.session_id = s.id
                                   AND m.role = 'user'
                                   AND m.content != ''
                                 ORDER BY m.id LIMIT 1), '') AS first_user
               FROM sessions s
               LEFT JOIN spaces sp ON sp.id = s.space_id
               WHERE s.session_type = 'normal'
                 AND s.created_at >= ?
                 AND NOT (s.title = 'New session' AND COALESCE(s.subtitle, '') = '')
                 AND (SELECT COUNT(*) FROM messages m
                       WHERE m.session_id = s.id
                         AND m.role = 'user'
                         AND m.content != '') >= 1
               ORDER BY s.created_at""",
            (cutoff_iso,),
        ).fetchall()
        candidates = [dict(r) for r in rows]
        if not candidates:
            return []
        # Scout reports carry the task_type label; the majority over a
        # session's rounds is the session's kind of work. One query for the
        # whole window, tallied in Python.
        scout_rows = conn.execute(
            """SELECT m.session_id, m.content FROM messages m
               WHERE m.role = 'scout'
                 AND m.session_id IN (
                     SELECT id FROM sessions
                     WHERE session_type = 'normal' AND created_at >= ?
                 )""",
            (cutoff_iso,),
        ).fetchall()

    tallies: dict[str, dict[str, int]] = {}
    for row in scout_rows:
        try:
            body = json.loads(row["content"])
        except ValueError:
            continue
        if not isinstance(body, dict):
            continue
        task_type = str(body.get("task_type") or "").strip()
        if not task_type:
            continue
        tallies.setdefault(row["session_id"], {})
        tallies[row["session_id"]][task_type] = tallies[row["session_id"]].get(task_type, 0) + 1
    for cand in candidates:
        counts = tallies.get(cand["id"])
        cand["task_type"] = max(counts, key=lambda k: (counts[k], k)) if counts else ""
    return candidates


# SQL predicate for "this session is not actively being worked on", for the
# background selectors (snooze distillation, refine, distill-coverage audit)
# that pick over old sessions. Requires the `sessions` table aliased as `s`.
#
# These selectors used to test the legacy `state = 'idle'` column. That column
# is no longer maintained by the state machine, so the test moved to state_v2.
# The membership list is the exact set the legacy column collapsed to "idle":
# CANCELLING/FINALIZING/AWAITING_* look busy but were indistinguishable from
# idle to the old column, and every one of these selectors additionally gates
# on `updated_at` being tens of minutes stale, so the transient members can
# never actually be live when a row is picked. NULL means the session predates
# migration v16 and has never transitioned since — idle by definition.
SQL_SESSION_IS_IDLE = (
    "(s.state_v2 IS NULL OR s.state_v2 IN "
    "('idle_ready', 'cancelling', 'finalizing', 'awaiting_user', 'awaiting_workers'))"
)


def get_sessions_in_state_v2(state_v2: str) -> list[dict]:
    """Return all sessions persisted with the given v2 state. Used by the
    boot-time reconcile sweep to find parents that were suspended on
    workers when the server stopped."""
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE state_v2 = ?",
            (state_v2,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_sessions_in_legacy_processing_only() -> list[dict]:
    """Return sessions where legacy state='processing' but state_v2 is NULL or empty.
    Used by the boot reconcile to catch sessions that crashed before state_v2 was written.

    Still live despite the state machine no longer writing the legacy column:
    migration v16 ADDs state_v2 without backfilling it, so any row that was
    stranded at state='processing' by a crash *before* v16 ran still has
    state_v2 NULL and is invisible to get_sessions_in_state_v2(). The boot
    reconcile stamps state_v2 on each row it finds, so the set drains to
    empty after one boot. Delete this (and its call site in
    SessionManager.reconcile_processing_sessions) once no deployed database
    — including restores from pre-v16 backups — can still contain a row with
    state='processing' AND state_v2 IS NULL."""
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE state = 'processing' AND (state_v2 IS NULL OR state_v2 = '')",
        ).fetchall()
        return [dict(r) for r in rows]


def delete_session(session_id: str) -> None:
    """Delete session and cascade (messages, artifacts, questions)."""
    # Delete workers first (recursive), then parent — all in one transaction
    with connect_sessions() as conn:
        worker_ids = [
            r["id"]
            for r in conn.execute("SELECT id FROM sessions WHERE parent_session_id = ?", (session_id,)).fetchall()
        ]
    for wid in worker_ids:
        delete_session(wid)
    with connect_sessions() as conn:
        # Single transaction: delete related rows then session.
        # messages_fts is a contentless-sync FTS table with no FK to messages —
        # without the explicit delete its rows leak forever (every weekly
        # cron-session prune grows the index and pollutes search results).
        conn.execute(
            "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM messages WHERE session_id = ?)",
            (session_id,),
        )
        conn.execute("DELETE FROM session_messages WHERE sender_id = ? OR recipient_id = ?", (session_id, session_id))
        # session_state_log has no FK to sessions, so its rows outlived every
        # deleted session. Cron/worker/canary sessions are deleted after a
        # week and each leaves 10-50 transitions behind forever, which made
        # this the largest table by row count and slowed the prune sweep
        # (whose own DISTINCT scan grows with it).
        conn.execute("DELETE FROM session_state_log WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def get_worker_sessions(parent_id: str) -> list[dict]:
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE parent_session_id = ? ORDER BY created_at",
            (parent_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Session state log (v13+)
# ---------------------------------------------------------------------------
# Append-only log of state-machine transitions. Written synchronously by
# sessions.state_v2.transition() under session.lock. See
# docs/internals/state-machine.md for the reason vocabulary.


def add_state_log(
    session_id: str,
    *,
    turn_id: int,
    from_state: str | None,
    to_state: str,
    reason: str,
    timestamp_ms: int,
    parent_turn_id: int | None = None,
    retry_index: int = 0,
    compaction_count: int = 0,
    termination_reason: str | None = None,
    reflect_count: int = 0,
    eval_count: int = 0,
    elapsed_ms: int | None = None,
) -> int:
    with connect_sessions() as conn:
        cur = conn.execute(
            """INSERT INTO session_state_log
               (session_id, turn_id, parent_turn_id, retry_index, compaction_count,
                from_state, to_state, reason, termination_reason,
                reflect_count, eval_count, timestamp_ms, elapsed_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                turn_id,
                parent_turn_id,
                retry_index,
                compaction_count,
                from_state,
                to_state,
                reason,
                termination_reason,
                reflect_count,
                eval_count,
                timestamp_ms,
                elapsed_ms,
            ),
        )
        return int(cur.lastrowid or 0)


def get_state_log(
    session_id: str,
    *,
    since_id: int = 0,
    before_id: int = 0,
    limit: int = 500,
    tail: bool = False,
) -> list[dict]:
    """Return state transitions for a session, oldest-first.

    Three windowing modes (rows are always returned oldest-first):
      * default      — the oldest `limit` rows with id > since_id.
      * tail=True    — the NEWEST `limit` rows (live views want the recent
                       end of a long log, not the start).
      * before_id>0  — the `limit` rows immediately preceding `before_id`
                       (backward pagination: "load older" from a tail view).
    """
    with connect_sessions() as conn:
        if before_id > 0:
            rows = conn.execute(
                """SELECT * FROM session_state_log
                   WHERE session_id = ? AND id < ?
                   ORDER BY id DESC
                   LIMIT ?""",
                (session_id, before_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
        if tail:
            rows = conn.execute(
                """SELECT * FROM session_state_log
                   WHERE session_id = ? AND id > ?
                   ORDER BY id DESC
                   LIMIT ?""",
                (session_id, since_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
        rows = conn.execute(
            """SELECT * FROM session_state_log
               WHERE session_id = ? AND id > ?
               ORDER BY id ASC
               LIMIT ?""",
            (session_id, since_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def latest_turn_id(session_id: str) -> int:
    """Max turn_id seen for this session, or 0 if no transitions logged."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(turn_id), 0) AS t FROM session_state_log WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["t"]) if row else 0


def prune_state_log(
    max_age_days: int = 30,
    keep_per_session: int = 500,
) -> int:
    """Retention: drop rows older than max_age_days, but always keep the
    most recent `keep_per_session` rows per session regardless of age.
    Returns count deleted."""
    import time

    cutoff_ms = int((time.time() - max_age_days * 86400) * 1000)
    with connect_sessions() as conn:
        # For each session, find the id threshold that would leave
        # keep_per_session rows. Anything below AND older than cutoff goes.
        rows = conn.execute(
            """SELECT session_id,
                      COALESCE(
                          (SELECT id FROM session_state_log s2
                           WHERE s2.session_id = s1.session_id
                           ORDER BY id DESC
                           LIMIT 1 OFFSET ?),
                          0) AS keep_floor
               FROM (SELECT DISTINCT session_id FROM session_state_log) s1""",
            (keep_per_session,),
        ).fetchall()
        total = 0
        for r in rows:
            sid = r["session_id"]
            floor = int(r["keep_floor"])
            if floor == 0:
                # Fewer rows than keep_per_session: the count floor does not
                # apply, but age still does. Without this, any session under
                # the floor was exempt from pruning forever regardless of how
                # old its rows were — which is most cron and worker sessions.
                cur = conn.execute(
                    "DELETE FROM session_state_log WHERE session_id = ? AND timestamp_ms < ?",
                    (sid, cutoff_ms),
                )
                total += cur.rowcount
                continue
            cur = conn.execute(
                """DELETE FROM session_state_log
                   WHERE session_id = ? AND id < ? AND timestamp_ms < ?""",
                (sid, floor, cutoff_ms),
            )
            total += cur.rowcount
        # Rows whose session is already gone (deleted before the cascade
        # above existed, or removed by a path that bypasses delete_session).
        total += conn.execute(
            "DELETE FROM session_state_log WHERE session_id NOT IN (SELECT id FROM sessions)"
        ).rowcount
        return total


# ---------------------------------------------------------------------------
# Turn records (the State timeline's read model)
# ---------------------------------------------------------------------------
# One record per turn, holding everything the agent produced inside it. The
# evidence is already persisted across three tables — session_state_log has
# the phases, messages has the scout/tool/reflect/eval/compaction/notice
# rows, token_usage has the cost — and the timeline modal used to fetch two
# of them whole and join them in the browser. This is that join, done once,
# server-side, over a bounded window. Nothing new is captured.
#
# The join is four bounded queries per page and no per-turn round trips:
# the turn-id page (a GROUP BY over one session's log), the full log rows
# for that contiguous span of turn ids, the messages inside the span's time
# window, and the token_usage rows in the same window.

# States the machine parks in between turns. A turn whose last transition
# lands in one of these is finished; anything else means it has no closing
# row (still running, or abandoned by a crash).
_TURN_IDLE_STATES = frozenset({"idle_ready", "awaiting_user", "awaiting_workers"})

# Roles whose body is structured and must be read whole. Everything else
# (tool results, assistant answers) is read as a 200-char head — only the
# error heuristic looks at it, and a single tool result can be 100s of KB.
_TURN_JSON_ROLES = ("scout", "reflect", "eval", "compaction", "notice")
_TURN_JSON_ROLES_SQL = "(" + ", ".join(f"'{r}'" for r in _TURN_JSON_ROLES) + ")"

_ARGS_SUMMARY_CAP = 160
_RAW_HEAD_CAP = 400
# A compaction summary that is prose rather than the usual fenced JSON is the
# whole record of what the turn carried forward; 400 characters would cut it
# off mid-sentence.
_SUMMARY_RAW_CAP = 4000
_CONTENT_HEAD_CAP = 200
_TOOL_CALLS_CAP = 65536
# How far before the oldest turn on the page to look for its opening prompt
# when there is no older turn to bound the search with.
_ROOT_LOOKBACK_MS = 60_000
# A turn's tail (post-hook rows) is written just after its closing
# transition; give the window that much slack when the next turn's start is
# unknown.
_TURN_TAIL_MS = 5_000

_TOOL_CALL_HEAD_RE = re.compile(r'"id"\s*:\s*"([^"]+)"\s*,\s*"name"\s*:\s*"([^"]+)"')


def _turn_ms_to_iso(ms: int | None) -> str | None:
    """Wall-clock milliseconds → the ISO stamp the API speaks."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _turn_iso_bound(ms: int) -> str:
    """A messages.created_at comparison bound.

    Written with an explicit 6-digit microsecond field rather than
    isoformat(), which drops the fraction entirely at a whole second — and
    "…:12+00:00" sorts BEFORE "…:12.468215+00:00", so a whole-second upper
    bound would silently exclude every row inside that second."""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _turn_usage_bound(ms: int) -> str:
    """A token_usage.created_at bound — SQLite's CURRENT_TIMESTAMP shape
    (naive UTC, second resolution), not the ISO stamp messages carry."""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _turn_stamp_to_ms(stamp) -> int | None:
    """Parse either created_at shape into wall-clock milliseconds.

    messages.created_at is an offset-aware ISO string; token_usage.created_at
    is SQLite's naive-UTC CURRENT_TIMESTAMP. Anything unparseable returns
    None and the row is skipped rather than raising."""
    if not stamp:
        return None
    text = str(stamp).strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _turn_json_body(raw: str | None) -> tuple[dict | None, str | None]:
    """Parse a structured message body. Returns (parsed, raw_head) with
    exactly one side filled in — a malformed body becomes a `raw` string on
    the record instead of a 500.

    Compaction summaries arrive as a ```json fence followed by a prose recap
    of the same thing, so the fenced region has to be cut out at its closing
    line: taking everything after the opening fence leaves the prose attached
    and json.loads reports "Extra data" on all but the rare summary that
    happens to end at the fence."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        close = text.find("\n```")
        if close != -1:
            text = text[:close]
        elif text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None, (raw or "")[:_RAW_HEAD_CAP]
    if not isinstance(parsed, dict):
        return None, (raw or "")[:_RAW_HEAD_CAP]
    return parsed, None


def _turn_args_summary(arguments) -> str:
    """A one-line "key: value, key: value" digest of a tool call's arguments,
    capped. Falls back to the raw argument string when it is not JSON (or
    was truncated out of parseability by the column read)."""
    if arguments in (None, ""):
        return ""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            return arguments[:_ARGS_SUMMARY_CAP]
    if not isinstance(arguments, dict):
        try:
            return json.dumps(arguments, default=str)[:_ARGS_SUMMARY_CAP]
        except (TypeError, ValueError):
            return str(arguments)[:_ARGS_SUMMARY_CAP]
    parts: list[str] = []
    used = 0
    for key, value in arguments.items():
        if isinstance(value, str):
            shown = value
        else:
            try:
                shown = json.dumps(value, default=str)
            except (TypeError, ValueError):
                shown = str(value)
        shown = shown[:_ARGS_SUMMARY_CAP].replace("\n", " ").replace("\r", " ")
        parts.append(f"{key}: {shown}")
        used += len(parts[-1]) + 2
        if used >= _ARGS_SUMMARY_CAP:
            break
    return ", ".join(parts)[:_ARGS_SUMMARY_CAP]


def _turn_looks_like_error(content: str | None) -> bool:
    """The pre-metadata error heuristic the timeline modal has always used.
    Only consulted for tool rows written before `metadata.was_error` existed
    — the stamp wins whenever it is there."""
    head = (content or "")[:120].lower()
    return head.startswith("error:") or "traceback" in head


def _turn_tool_call_index(rows: list[dict]) -> dict:
    """tool_call_id → {name, arguments, issued_at} from the assistant rows
    that issued them (core/agent.py stores [{id, name, arguments}]).

    The column read is capped, so a call carrying a large file body can come
    back as truncated JSON; the regex recovers at least the id and name from
    the head of each object rather than losing the row's identity."""
    index: dict = {}
    for row in rows:
        if row["role"] != "assistant" or not row["tool_calls"]:
            continue
        raw = row["tool_calls"]
        try:
            calls = json.loads(raw)
        except (ValueError, TypeError):
            calls = [{"id": cid, "name": name} for cid, name in _TOOL_CALL_HEAD_RE.findall(raw)]
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict) or not call.get("id"):
                continue
            index[call["id"]] = {
                "name": call.get("name") or "tool",
                "arguments": call.get("arguments"),
                "issued_at": row["_ms"],
            }
    return index


def _turn_assign(
    msgs: list[dict], starts: list[int], his: list[int], next_start_ms: int | None, win_lo_ms: int
) -> None:
    """Stamp each message with the index of the turn it belongs to (`_turn`),
    or None when it belongs to a turn outside this page.

    A message belongs to the turn whose [start, end] contains it (end = now
    for the running turn). Between two turns the discriminator is role — a
    `user` row in the gap is the prompt that opened the NEXT turn (it is
    written a millisecond or two before the transition it triggers), and
    everything else is the previous turn's post-hook tail: a deferred grade,
    a notice, a late tool row.

    The window is the whole rule, deliberately. `metadata.parent_user_msg_id`
    looks like a better key — it names the user message a row was produced
    under — but it is not one: `current_turn_user_msg_id` survives turns that
    never refresh it, so on a real session (e058985e52df) one user message id
    is stamped as the parent of four separate turns. Keying on it collapses
    those four turns' work into whichever one claims the id first. It stays
    useful for what it was added for — sorting an injected row inside its
    turn — but not for saying which turn a row belongs to.

    Also called for token_usage rows, which have no role at all; those can
    never be a turn's opening prompt, and `.get` is what says so."""
    last = len(starts) - 1
    for msg in msgs:
        ts = msg["_ms"]
        if ts is None:
            msg["_turn"] = None
            continue
        idx = bisect_right(starts, ts) - 1
        if idx < 0:
            # Ahead of the page's oldest turn: only its own prompt counts.
            msg["_turn"] = 0 if (msg.get("role") == "user" and ts >= win_lo_ms) else None
            continue
        if ts <= his[idx]:
            msg["_turn"] = idx
            continue
        if idx < last:
            msg["_turn"] = idx + 1 if msg.get("role") == "user" else idx
            continue
        if next_start_ms is None:
            msg["_turn"] = idx  # tail of the session's newest turn
        elif msg.get("role") == "user":
            msg["_turn"] = None  # the next turn's prompt, not ours
        else:
            msg["_turn"] = idx if ts < next_start_ms else None


def _turn_phases(rows: list[dict], closed: bool, end_ms: int) -> list[dict]:
    """The states the turn passed through, in order, with the wall-clock time
    spent in each.

    A log row records a transition, so state N's span runs from the row that
    entered it to the row that left it. Durations come from the timestamp
    delta rather than the row's own `elapsed_ms`, which is measured off a
    monotonic clock that resets when the process restarts; the delta keeps
    the phases summing exactly to the turn's elapsed. The turn's opening row
    is excluded — its `elapsed_ms` is time spent idle before the prompt."""
    phases: list[dict] = []
    for i in range(len(rows) - 1):
        head, nxt = rows[i], rows[i + 1]
        phases.append(
            {
                "state": head["to_state"],
                "started_at": _turn_ms_to_iso(head["timestamp_ms"]),
                "ended_at": _turn_ms_to_iso(nxt["timestamp_ms"]),
                "elapsed_ms": max(0, nxt["timestamp_ms"] - head["timestamp_ms"]),
                "reason_in": head["reason"],
                "reason_out": nxt["reason"],
            }
        )
    if not closed and rows:
        tail = rows[-1]
        phases.append(
            {
                "state": tail["to_state"],
                "started_at": _turn_ms_to_iso(tail["timestamp_ms"]),
                "ended_at": None,
                "elapsed_ms": max(0, end_ms - tail["timestamp_ms"]),
                "reason_in": tail["reason"],
                "reason_out": None,
            }
        )
    return phases


def _turn_tokens(usage: list[dict]) -> dict:
    """Token totals for one turn. `cost_estimate` stays null when no row
    priced itself — an unpriced local model must not report a cost of 0."""
    priced = [u["cost_estimate"] for u in usage if u["cost_estimate"] is not None]
    return {
        "prompt": sum(int(u["prompt_tokens"] or 0) for u in usage),
        "completion": sum(int(u["completion_tokens"] or 0) for u in usage),
        "total": sum(int(u["total_tokens"] or 0) for u in usage),
        "calls": len(usage),
        "cost_estimate": sum(priced) if priced else None,
        "models": sorted({u["model"] for u in usage if u["model"]}),
    }


def _turn_record(page_turn: dict, rows: list[dict], msgs: list[dict], usage: list[dict], now_ms: int) -> dict:
    """Fold one turn's state-log rows, messages and usage rows into a record."""
    first, last = rows[0], rows[-1]
    closed = last["to_state"] in _TURN_IDLE_STATES
    running = page_turn["_running"]
    end_ms = now_ms if running else last["timestamp_ms"]

    violations = [r["reason"] for r in rows if str(r["reason"] or "").startswith("invariant-violation")]
    if not closed and not running:
        # No closing transition and no longer the live turn: the process died
        # mid-turn. Record it rather than letting the turn read as finished.
        violations.append("turn-never-closed")

    termination = None
    for row in rows:
        if row["termination_reason"]:
            termination = row["termination_reason"]

    tool_index = _turn_tool_call_index(msgs)
    tool_calls: list[dict] = []
    scouts: list[dict] = []
    reflects: list[dict] = []
    evals: list[dict] = []
    compactions: list[dict] = []
    notices: list[dict] = []
    model_votes: dict[str, int] = {}

    for msg in msgs:
        role = msg["role"]
        if role == "assistant":
            model = (msg["_meta"] or {}).get("model")
            if isinstance(model, str) and model:
                model_votes[model] = model_votes.get(model, 0) + 1
        elif role == "tool":
            meta = msg["_meta"] or {}
            call = tool_index.get(msg["tool_call_id"]) or {}
            latency = msg["latency_ms"]
            if latency is None:
                latency = meta.get("latency_ms")
            if "was_error" in meta:
                was_error = bool(meta["was_error"])
            else:
                was_error = _turn_looks_like_error(msg["content"])
            started = call.get("issued_at")
            if started is None:
                started = msg["_ms"] - int(latency or 0) if msg["_ms"] is not None else None
            tool_calls.append(
                {
                    "message_id": msg["id"],
                    "call_id": msg["tool_call_id"],
                    "name": call.get("name") or "tool",
                    "args_summary": _turn_args_summary(call.get("arguments")),
                    "latency_ms": int(latency) if latency is not None else None,
                    "was_error": was_error,
                    "started_at": _turn_ms_to_iso(started),
                }
            )
        elif role == "scout":
            body, raw = _turn_json_body(msg["content"])
            scouts.append({"raw": raw} if body is None else body)
        elif role == "reflect":
            body, raw = _turn_json_body(msg["content"])
            entry = {"attempt": len(reflects) + 1}
            if body is None:
                entry["raw"] = raw
            else:
                for key in ("verdict", "reasoning", "diagnostic", "what_worked"):
                    entry[key] = body.get(key)
            reflects.append(entry)
        elif role == "eval":
            body, raw = _turn_json_body(msg["content"])
            if body is None:
                evals.append({"attempt": len(evals) + 1, "gates": [], "raw": raw})
                continue
            gates = body.get("gates")
            evals.append(
                {
                    "attempt": body.get("attempt", len(evals) + 1),
                    "gates": [
                        {
                            "name": g.get("name"),
                            "command": g.get("command"),
                            "passed": bool(g.get("passed")),
                            "exit_code": g.get("exit_code"),
                            "output_tail": g.get("output_tail") or "",
                        }
                        for g in (gates if isinstance(gates, list) else [])
                        if isinstance(g, dict)
                    ],
                }
            )
        elif role == "compaction":
            body, raw = _turn_json_body(msg["content"])
            meta = msg["_meta"] or {}
            compactions.append(
                {
                    "summary": body if body is not None else (msg["content"] or "")[:_SUMMARY_RAW_CAP],
                    "compacted_up_to": meta.get("compacted_up_to"),
                    "original_count": meta.get("original_count"),
                    "at": _turn_ms_to_iso(msg["_ms"]),
                }
            )
        elif role == "notice":
            notices.append({"text": msg["content"] or "", "at": _turn_ms_to_iso(msg["_ms"])})

    return {
        "turn_id": first["turn_id"],
        "parent_turn_id": first["parent_turn_id"],
        "retry_index": max(int(r["retry_index"] or 0) for r in rows),
        "running": running,
        "started_at": _turn_ms_to_iso(first["timestamp_ms"]),
        "ended_at": None if running else _turn_ms_to_iso(last["timestamp_ms"]),
        "elapsed_ms": max(0, end_ms - first["timestamp_ms"]),
        "termination_reason": termination,
        "reflect_count": max(int(r["reflect_count"] or 0) for r in rows),
        "eval_count": max(int(r["eval_count"] or 0) for r in rows),
        "compaction_count": max(int(r["compaction_count"] or 0) for r in rows),
        "phases": _turn_phases(rows, closed, end_ms),
        "tool_calls": tool_calls,
        # Retries re-scout; the report kept is the one the turn opened with,
        # which is the plan reflect[0] graded. The extra scouting phases stay
        # visible in `phases`.
        "scout": scouts[0] if scouts else None,
        "reflect": reflects,
        "eval": evals,
        "compactions": compactions,
        "notices": notices,
        "tokens": _turn_tokens(usage),
        "model": max(model_votes, key=lambda m: (model_votes[m], m)) if model_votes else None,
        "invariant_violations": violations,
    }


def get_turns(session_id: str, before_turn: int = 0, limit: int = 20) -> dict:
    """One record per turn for the State timeline, newest turn first.

    `before_turn` pages backward (turns older than that turn id); `limit` is
    clamped to 1..100. `has_more` says whether an older page exists. A
    session with no transitions logged returns an empty page rather than an
    error — the 404 for an unknown session belongs to the route."""
    limit = max(1, min(20 if limit is None else int(limit), 100))
    before_turn = max(0, 0 if before_turn is None else int(before_turn))
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    with connect_sessions() as conn:
        newest_row = conn.execute(
            "SELECT COALESCE(MAX(turn_id), 0) AS t FROM session_state_log WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        newest_turn = int(newest_row["t"]) if newest_row else 0

        # One row per turn: the page itself, plus the turn the caller paged
        # back from (its start bounds our newest turn's tail) and one turn
        # behind the page (its end bounds our oldest turn's prompt search).
        bounds = [
            dict(r)
            for r in conn.execute(
                """SELECT turn_id,
                          MIN(timestamp_ms) AS start_ms,
                          MAX(timestamp_ms) AS end_ms
                   FROM session_state_log
                   WHERE session_id = ? AND (? = 0 OR turn_id <= ?)
                   GROUP BY turn_id
                   ORDER BY turn_id DESC
                   LIMIT ?""",
                (session_id, before_turn, before_turn, limit + 2),
            )
        ]
        above = bounds.pop(0) if (before_turn and bounds and bounds[0]["turn_id"] == before_turn) else None
        page_desc = bounds[:limit]
        has_more = len(bounds) > limit
        below = bounds[limit] if has_more else None
        if not page_desc:
            return {"session_id": session_id, "count": 0, "has_more": False, "turns": []}

        page = list(reversed(page_desc))
        log_rows = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM session_state_log
                   WHERE session_id = ? AND turn_id >= ? AND turn_id <= ?
                   ORDER BY id""",
                (session_id, page[0]["turn_id"], page[-1]["turn_id"]),
            )
        ]

        # Only the session's newest turn can still be running, and only when
        # the caller is not paging behind it.
        for entry in page:
            entry["_running"] = False
        newest_entry = page[-1]
        newest_is_live = above is None and newest_entry["turn_id"] >= newest_turn
        newest_rows = [r for r in log_rows if r["turn_id"] == newest_entry["turn_id"]]
        if newest_is_live and newest_rows and newest_rows[-1]["to_state"] not in _TURN_IDLE_STATES:
            newest_entry["_running"] = True

        starts = [t["start_ms"] for t in page]
        his = [now_ms if t["_running"] else t["end_ms"] for t in page]
        if above is not None:
            next_start_ms: int | None = above["start_ms"]
        elif newest_is_live:
            next_start_ms = None
        else:
            next_start_ms = newest_entry["end_ms"] + _TURN_TAIL_MS
        win_lo_ms = below["end_ms"] if below else starts[0] - _ROOT_LOOKBACK_MS
        win_hi_ms = next_start_ms if next_start_ms is not None else now_ms

        msgs = [
            dict(r)
            for r in conn.execute(
                f"""SELECT id, role, tool_call_id, latency_ms, metadata, created_at,
                           substr(tool_calls, 1, {_TOOL_CALLS_CAP}) AS tool_calls,
                           CASE WHEN role IN {_TURN_JSON_ROLES_SQL}
                                THEN content
                                ELSE substr(content, 1, {_CONTENT_HEAD_CAP}) END AS content
                    FROM messages
                    WHERE session_id = ? AND created_at >= ? AND created_at <= ?
                    ORDER BY id""",
                (session_id, _turn_iso_bound(win_lo_ms), _turn_iso_bound(win_hi_ms + 1000)),
            )
        ]
        usage_rows = [
            dict(r)
            for r in conn.execute(
                """SELECT model, prompt_tokens, completion_tokens, total_tokens,
                          cost_estimate, created_at
                   FROM token_usage
                   WHERE session_id = ? AND created_at >= ? AND created_at <= ?""",
                (session_id, _turn_usage_bound(win_lo_ms), _turn_usage_bound(win_hi_ms + 1000)),
            )
        ]

    for msg in msgs:
        msg["_ms"] = _turn_stamp_to_ms(msg["created_at"])
        msg["_meta"], _ = _turn_json_body(msg["metadata"])
    _turn_assign(msgs, starts, his, next_start_ms, win_lo_ms)

    for row in usage_rows:
        row["_ms"] = _turn_stamp_to_ms(row["created_at"])
    _turn_assign(usage_rows, starts, his, next_start_ms, win_lo_ms)

    by_turn: dict[int, list[dict]] = {t["turn_id"]: [] for t in page}
    for row in log_rows:
        if row["turn_id"] in by_turn:
            by_turn[row["turn_id"]].append(row)
    msgs_by_turn: dict[int, list[dict]] = {t["turn_id"]: [] for t in page}
    for msg in msgs:
        if msg["_turn"] is not None:
            msgs_by_turn[page[msg["_turn"]]["turn_id"]].append(msg)
    usage_by_turn: dict[int, list[dict]] = {t["turn_id"]: [] for t in page}
    for row in usage_rows:
        if row["_turn"] is not None:
            usage_by_turn[page[row["_turn"]]["turn_id"]].append(row)

    turns = [
        _turn_record(
            entry, by_turn[entry["turn_id"]], msgs_by_turn[entry["turn_id"]], usage_by_turn[entry["turn_id"]], now_ms
        )
        for entry in reversed(page)
        if by_turn[entry["turn_id"]]
    ]
    return {"session_id": session_id, "count": len(turns), "has_more": has_more, "turns": turns}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def add_message(
    session_id: str,
    role: str,
    content: str = "",
    tool_call_id: str | None = None,
    tool_calls: str | None = None,
    partial: int = 0,
    token_count: int | None = None,
    idempotency_key: str | None = None,
    latency_ms: int | None = None,
    metadata: str | None = None,
) -> int:
    """Insert a message. Returns message ID."""
    with connect_sessions() as conn:
        cur = conn.execute(
            """INSERT INTO messages (session_id, role, content, tool_call_id,
               tool_calls, char_count, token_count, partial, idempotency_key,
               latency_ms, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                role,
                content,
                tool_call_id,
                tool_calls,
                len(content),
                token_count,
                partial,
                idempotency_key,
                latency_ms,
                metadata,
                _now(),
            ),
        )
        msg_id = cur.lastrowid
        # Keep messages_fts in sync
        if role in ("user", "assistant", "tool") and len(content) > 10:
            try:
                conn.execute(
                    "INSERT INTO messages_fts (rowid, session_id, role, content) VALUES (?, ?, ?, ?)",
                    (msg_id, session_id, role, content),
                )
            except Exception as e:
                logger.debug("FTS insert skipped for message %d: %s", msg_id, e)
        return msg_id


def get_messages(session_id: str, last: int | None = None, before_id: int | None = None) -> list[dict]:
    """Return messages oldest-first. With `last`, only the newest N rows
    (still oldest-first) — tail consumers (reflect, post-hooks, approval
    checks) should pass it instead of loading the whole transcript: this
    runs synchronously on the event loop and tool results can be 100s of
    KB each. `before_id` (with `last`) pages further back: the newest N
    rows whose id is < before_id."""
    with connect_sessions() as conn:
        if last is not None:
            if before_id is not None:
                rows = conn.execute(
                    """SELECT * FROM (
                           SELECT * FROM messages WHERE session_id = ? AND id < ?
                           ORDER BY created_at DESC, id DESC LIMIT ?
                       ) ORDER BY created_at, id""",
                    (session_id, before_id, last),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM (
                           SELECT * FROM messages WHERE session_id = ?
                           ORDER BY created_at DESC, id DESC LIMIT ?
                       ) ORDER BY created_at, id""",
                    (session_id, last),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def count_messages(session_id: str, before_id: int | None = None) -> int:
    """How many messages the session holds. With `before_id`, only those
    older than that row — which is exactly "is there another page behind
    this one" for the transcript's prepend-only paging."""
    with connect_sessions() as conn:
        if before_id is not None:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ? AND id < ?",
                (session_id, before_id),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (session_id,)).fetchone()
        return int(row["c"]) if row else 0


def get_last_message_at(session_id: str, role: str) -> str | None:
    """Return the created_at of the newest message with the given role
    (None if there are none). Cheap indexed lookup — avoids loading the
    whole transcript just to find one timestamp."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS ts FROM messages WHERE session_id = ? AND role = ?",
            (session_id, role),
        ).fetchone()
        return row["ts"] if row and row["ts"] else None


def get_message(message_id: int) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None


def user_messages_after(session_id: str, after_id: int, limit: int = 3) -> list[dict]:
    """The next few user-role messages strictly after `after_id`, oldest first.

    The deferred grader reads this to find the message that followed the turn
    it is grading — the user's own reaction is the cheapest ground truth the
    loop has. Bounded by `limit` because the caller only ever wants the first
    non-synthetic one; harness-authored user rows (worker-resume injections)
    are filtered by the caller, which owns that vocabulary.
    """
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT * FROM messages
               WHERE session_id = ? AND id > ? AND role = 'user'
               ORDER BY id LIMIT ?""",
            (session_id, int(after_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def last_message_id(session_id: str) -> int | None:
    """Highest message id in the session, or None when it has no messages.

    Used as the closing bound of a turn's message-id range: captured when a
    deferred grade is scheduled so the evidence slice stays fixed even after
    the next turn has appended to the transcript.
    """
    with connect_sessions() as conn:
        row = conn.execute("SELECT MAX(id) AS m FROM messages WHERE session_id = ?", (session_id,)).fetchone()
    return int(row["m"]) if row and row["m"] is not None else None


def turn_has_final_answer(session_id: str, user_msg_id: int) -> bool:
    """True if the turn rooted at `user_msg_id` already wrote a text answer.

    A "final answer" is an assistant row carrying content but no tool_calls,
    tagged (via metadata.parent_user_msg_id, stamped by run_agent's
    _save_turn_msg) as belonging to this turn. Used by the rapid-fire
    combiner to decide whether folding a follow-up into this turn's user
    row can still be seen by the running agent loop.

    Deliberately conservative: the max_tokens length-continuation path also
    writes a no-tool_calls assistant row mid-turn, so this can read True
    slightly early. The caller treats True as "do not combine, queue a new
    turn instead", which is the safe direction — an extra turn rather than a
    silently dropped message.
    """
    with connect_sessions() as conn:
        row = conn.execute(
            """SELECT 1 FROM messages
               WHERE session_id = ?
                 AND role = 'assistant'
                 AND (tool_calls IS NULL OR tool_calls = '')
                 AND length(COALESCE(content, '')) > 0
                 AND json_extract(metadata, '$.parent_user_msg_id') = ?
               LIMIT 1""",
            (session_id, user_msg_id),
        ).fetchone()
        return row is not None


def get_orphaned_user_messages(session_id: str) -> list[dict]:
    """Return user messages that have no subsequent assistant response.

    Scans the session's message sequence and returns any user message that is
    followed by another user message (or end-of-session) without an assistant
    message in between. These are turns the agent never handled — typically
    because the server restarted between the message being queued and
    _process_pending draining it.

    Injected user messages (metadata.injected=true) are skipped because they
    were inserted for mid-turn context, not as new conversation turns.
    """
    import json as _json

    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    orphans: list[dict] = []
    pending_user: dict | None = None
    for r in rows:
        m = dict(r)
        role = m.get("role", "")
        if role == "user":
            try:
                meta = _json.loads(m.get("metadata") or "{}")
                if meta.get("injected"):
                    continue
            except Exception:
                pass
            if pending_user is not None:
                orphans.append(pending_user)
            pending_user = m
        elif role == "assistant":
            pending_user = None
    if pending_user is not None:
        orphans.append(pending_user)
    return orphans


def delete_message(message_id: int) -> None:
    with connect_sessions() as conn:
        # Delete FTS entry first — if it fails, the message is still intact
        try:
            conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (message_id,))
        except Exception as e:
            logger.warning("FTS delete failed for message %d: %s", message_id, e)
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))


def delete_messages_from(session_id: str, from_id: int) -> None:
    """Delete all messages in session with id >= from_id."""
    with connect_sessions() as conn:
        try:
            conn.execute(
                "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM messages WHERE session_id = ? AND id >= ?)",
                (session_id, from_id),
            )
        except Exception as e:
            logger.warning("FTS batch delete failed for session %s from id %d: %s", session_id, from_id, e)
        conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND id >= ?",
            (session_id, from_id),
        )


def update_message_content(message_id: int, content: str) -> None:
    """Replace a message's content in place. Keeps FTS index in sync.

    Used by the rapid-fire queue combiner: when several user messages land
    within a short window, the queue collapses them into one DB row whose
    content is rewritten to the formatted combined form. This is the one
    sanctioned mutation of a message body — all other writes go through
    add_message (append-only).
    """
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE messages SET content = ?, char_count = ? WHERE id = ?",
            (content, len(content), message_id),
        )
        # Keep FTS in sync — INSERT-OR-REPLACE is simpler than DELETE+INSERT
        try:
            conn.execute(
                "INSERT OR REPLACE INTO messages_fts (rowid, session_id, role, content) "
                "SELECT id, session_id, role, content FROM messages WHERE id = ?",
                (message_id,),
            )
        except Exception as e:
            logger.debug("FTS update skipped for message %d: %s", message_id, e)


def get_last_partial(session_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND partial = 1 ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None


def add_compaction(session_id: str, summary: str, compacted_up_to: int, original_count: int) -> int:
    """Add a compaction marker message with metadata in dedicated column."""
    import json

    meta = json.dumps(
        {
            "compacted_up_to": compacted_up_to,
            "original_count": original_count,
        }
    )
    with connect_sessions() as conn:
        cur = conn.execute(
            """INSERT INTO messages (session_id, role, content, metadata,
               char_count, partial, created_at)
               VALUES (?, 'compaction', ?, ?, ?, 0, ?)""",
            (session_id, summary, meta, len(summary), _now()),
        )
        return cur.lastrowid


def clear_messages_only(session_id: str) -> None:
    """Clear all messages but keep session and artifacts."""
    with connect_sessions() as conn:
        try:
            conn.execute(
                "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM messages WHERE session_id = ?)",
                (session_id,),
            )
        except Exception:
            pass
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))


def search_messages_fts(
    query: str,
    limit: int = 15,
    exclude_session: str = "",
    include_session: str = "",
) -> list[dict]:
    """FTS5 search across session messages. Returns ranked results with session context.

    Filter semantics (mutually exclusive — `include_session` wins if both set):
      - include_session=""   → search all sessions (with optional exclude_session)
      - include_session=<id> → restrict to that single session (exclude_session ignored)
    """
    import logging
    import re

    # Wrap each word in `"..."` so FTS5 treats it as a literal phrase. This
    # neutralises every operator char in one shot: `:` (column syntax),
    # `-` (NOT, e.g. `Re-run` → "no such column: run"), `.` (syntax error),
    # `/`, `=`, `%`, `?`, plus the AND/OR/NOT/NEAR keywords. We still strip
    # the quote/bracket family because they break the wrapper itself, and
    # collapse commas to spaces so `"a,b"` doesn't become one phrase.
    clean = re.sub(r'["\'()\[\]{}]', " ", query)
    clean = clean.replace(",", " ")
    words = [w for w in clean.split() if len(w) >= 2]
    if not words:
        return []
    fts_query = " OR ".join(f'"{w}"' for w in words)

    log = logging.getLogger("pernix.db")
    if include_session and exclude_session:
        # include wins; surface a soft warning so callers learn the precedence.
        log.warning("search_messages_fts called with both include_session and exclude_session — include wins")

    with connect_sessions() as conn:
        sql = """
            SELECT f.rowid as msg_id, f.session_id, f.role, f.content,
                   s.title as session_title, s.session_type,
                   s.space_id as session_space_id,
                   s.archived_at as session_archived_at,
                   s.created_at as session_created_at,
                   s.updated_at as session_updated_at,
                   bm25(messages_fts, 1.0) as score
            FROM messages_fts f
            LEFT JOIN sessions s ON f.session_id = s.id
            WHERE messages_fts MATCH ?
              AND f.role IN ('user', 'assistant', 'tool')
              AND (s.session_type IS NULL OR s.session_type NOT IN ('canary'))
        """
        params: list = [fts_query]

        if include_session:
            sql += " AND f.session_id = ?"
            params.append(include_session)
        elif exclude_session:
            sql += " AND f.session_id != ?"
            params.append(exclude_session)

        sql += " ORDER BY score LIMIT ?"
        params.append(limit)

        try:
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "msg_id": r["msg_id"],
                    "session_id": r["session_id"],
                    "session_title": r["session_title"] or "untitled",
                    "session_type": r["session_type"] or "normal",
                    "session_space_id": r["session_space_id"],
                    # Search is the one surface an archived session still
                    # appears on — that is the promise archiving makes — so
                    # the hit has to say which of them are archived.
                    "session_archived": bool(r["session_archived_at"]),
                    "session_created_at": (r["session_created_at"] or "")[:16],
                    "session_updated_at": (r["session_updated_at"] or "")[:16],
                    "role": r["role"],
                    "content": (r["content"] or "")[:300],
                    "score": abs(r["score"]),
                }
                for r in rows
            ]
        except sqlite3.OperationalError as e:
            # Most likely FTS5 query syntax error (rare special chars slip through)
            # or missing FTS table on a fresh DB. Log so the agent isn't lied to
            # via a silent empty result.
            log.warning("search_messages_fts SQL error (query=%r): %s", fts_query, e)
            return []
        except Exception as e:
            log.warning("search_messages_fts unexpected error: %s", e)
            return []


def resolve_session_id(prefix_or_id: str) -> str | None:
    """Resolve a session id, accepting either the full id or an unambiguous prefix.

    Returns the full session id on a unique match, or None if the prefix matches
    zero or more than one session. Used by tools that accept agent-supplied ids
    where the agent may copy back the 8/12-char prefix it saw in tool output.
    """
    if not prefix_or_id:
        return None
    with connect_sessions() as conn:
        # Exact match wins regardless of length.
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (prefix_or_id,)).fetchone()
        if row:
            return row["id"]
        # Prefix match — only accept unambiguous resolution.
        rows = conn.execute(
            "SELECT id FROM sessions WHERE id LIKE ? LIMIT 2",
            (prefix_or_id + "%",),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        return None


def recent_termination_reasons(session_id: str, limit: int = 3) -> list[str]:
    """Most recent N non-null termination_reason values for the session, newest-first.

    Used by reflect to detect ceiling-loops: if termination_reason has been
    'round_ceiling' on the current turn AND a prior turn, the agent is hitting
    the same hard wall and reflect should escalate.
    """
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT termination_reason FROM session_state_log
               WHERE session_id = ? AND termination_reason IS NOT NULL
               ORDER BY id DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [r["termination_reason"] for r in rows]


def ledger_anchor(session_id: str, before_msg_id: int) -> str | None:
    """Timestamp of the last message before this turn's user message — the
    'since when' for the turn-boundary ledger. None on a session's first
    turn (the caller falls back to the session's created_at)."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT created_at FROM messages WHERE session_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
            (session_id, before_msg_id),
        ).fetchone()
        return row["created_at"] if row else None


def ledger_snapshot(session_id: str, anchor_iso: str) -> dict:
    """Everything that changed around this session since `anchor_iso` — the
    read side of the turn-boundary ledger (agent-ergonomics plan, Tier 1).

    Every group is bounded and best-effort: a group whose table or column is
    missing (older DB, feature off) contributes an empty list rather than
    sinking the snapshot. All data already exists in these tables; this is
    composition, not new plumbing.
    """
    out: dict = {
        "finished_workers": [],
        "finished_jobs": [],
        "finished_rlm": [],
        "inflight": {},
        "last_verdict": None,
        "open_questions": [],
        "agent_proposals": [],
        "adaptive_changes": [],
        "canary_fails": [],
        "boot": {},
    }
    with connect_sessions() as conn:
        try:
            out["finished_workers"] = [
                dict(r)
                for r in conn.execute(
                    """SELECT id, title, updated_at FROM sessions
                       WHERE parent_session_id = ? AND session_type = 'worker'
                         AND state_v2 = 'idle_ready' AND updated_at > ?
                       ORDER BY updated_at DESC LIMIT 4""",
                    (session_id, anchor_iso),
                ).fetchall()
            ]
        except Exception:
            pass
        try:
            out["finished_jobs"] = [
                dict(r)
                for r in conn.execute(
                    """SELECT id, name, command, state, exit_code FROM jobs
                       WHERE session_id = ? AND finished_at > ?
                       ORDER BY finished_at DESC LIMIT 3""",
                    (session_id, anchor_iso),
                ).fetchall()
            ]
        except Exception:
            pass
        try:
            out["finished_rlm"] = [
                dict(r)
                for r in conn.execute(
                    """SELECT run_id, status FROM rlm_runs
                       WHERE session_id = ? AND finished_at > ?
                       ORDER BY finished_at DESC LIMIT 2""",
                    (session_id, anchor_iso),
                ).fetchall()
            ]
        except Exception:
            pass
        try:
            busy = "('scouting', 'processing', 'compacting', 'awaiting_workers')"
            out["inflight"] = {
                "workers": conn.execute(
                    f"SELECT COUNT(*) c FROM sessions WHERE session_type = 'worker' AND state_v2 IN {busy}"
                ).fetchone()["c"],
                "jobs": conn.execute("SELECT COUNT(*) c FROM jobs WHERE state = 'running'").fetchone()["c"],
                "rlm": conn.execute("SELECT COUNT(*) c FROM rlm_runs WHERE status = 'running'").fetchone()["c"],
                "cron": conn.execute(
                    f"SELECT COUNT(*) c FROM sessions WHERE session_type = 'cron' AND state_v2 IN {busy}"
                ).fetchone()["c"],
            }
        except Exception:
            pass
        try:
            row = conn.execute(
                """SELECT verdict, failure_cause, payload_json, created_at FROM post_mortems
                   WHERE session_id = ? AND created_at > ?
                   ORDER BY created_at DESC LIMIT 1""",
                (session_id, anchor_iso),
            ).fetchone()
            out["last_verdict"] = dict(row) if row else None
        except Exception:
            pass
        try:
            out["open_questions"] = [
                dict(r)
                for r in conn.execute(
                    """SELECT question, created_at FROM questions
                       WHERE session_id = ? AND answered_at IS NULL
                       ORDER BY created_at DESC LIMIT 2""",
                    (session_id,),
                ).fetchall()
            ]
        except Exception:
            pass
        try:
            out["agent_proposals"] = [
                dict(r)
                for r in conn.execute(
                    """SELECT id, created_at FROM adaptive_proposals
                       WHERE status = 'pending' AND producer = 'agent'
                       ORDER BY created_at DESC LIMIT 2""",
                ).fetchall()
            ]
        except Exception:
            pass
        try:
            out["adaptive_changes"] = [
                dict(r)
                for r in conn.execute(
                    """SELECT entry_id, action, actor FROM adaptive_events
                       WHERE created_at > ? AND action IN ('create', 'update', 'delete')
                       ORDER BY id DESC LIMIT 6""",
                    (anchor_iso,),
                ).fetchall()
            ]
        except Exception:
            pass
        try:
            out["canary_fails"] = [
                dict(r)
                for r in conn.execute(
                    """SELECT task, created_at FROM canary_runs
                       WHERE created_at > ? AND outcome = 'gate_fail'
                       ORDER BY created_at DESC LIMIT 3""",
                    (anchor_iso,),
                ).fetchall()
            ]
        except Exception:
            pass
    try:
        out["boot"] = {
            "at": get_snooze_state("app_last_boot_at") or "",
            "was_deploy": get_snooze_state("app_last_boot_was_deploy") == "1",
            "stamp": get_snooze_state("app_version_seen") or "",
        }
    except Exception:
        pass
    return out


def get_message_context(session_id: str, message_id: int, window: int = 2) -> list[dict]:
    """Get a message and its surrounding context (window messages before/after)."""
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT id, role, content FROM messages
               WHERE session_id = ? AND id BETWEEN ? AND ?
               AND role IN ('user', 'assistant', 'tool')
               ORDER BY id""",
            (session_id, message_id - window, message_id + window),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Token Usage
# ---------------------------------------------------------------------------


def add_token_usage(
    session_id: str,
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_estimate: float | None = None,
    source: str = "provider",
    provider: str = "",
    goal_id: int | None = None,
) -> None:
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO token_usage (session_id, model, prompt_tokens,
               completion_tokens, total_tokens, cache_read_tokens,
               cache_write_tokens, cost_estimate, source, provider, goal_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                cache_read_tokens,
                cache_write_tokens,
                cost_estimate,
                source,
                provider,
                goal_id,
            ),
        )


def get_session_usage(session_id: str) -> dict:
    with connect_sessions() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(prompt_tokens), 0) as prompt,
                      COALESCE(SUM(completion_tokens), 0) as completion,
                      COALESCE(SUM(total_tokens), 0) as total,
                      COALESCE(SUM(cache_read_tokens), 0) as cache_read,
                      COALESCE(SUM(cache_write_tokens), 0) as cache_write,
                      COALESCE(SUM(cost_estimate), 0) as cost,
                      COUNT(*) as calls
               FROM token_usage WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        return dict(row) if row else {}


def session_token_usage_since(session_id: str, since_iso: str, until_iso: str = "") -> dict:
    """Token totals for one session inside a time window — the turn window.

    Reflect stamps this into the post-mortem as turn_metrics: the turn's
    user message created_at is the start anchor (retries included — they
    are part of what the turn cost) and the turn's LAST message is the end
    anchor. The end bound matters for deferred reflect, which grades
    minutes after the turn: without it the window would swallow the
    deferred delay and any next turn already running.
    token_usage.created_at is a SQLite CURRENT_TIMESTAMP ("YYYY-MM-DD
    HH:MM:SS", UTC); message stamps are ISO with offset — normalize the
    anchors to the same shape for comparison.
    """
    anchor = str(since_iso).replace("T", " ")[:19]
    clauses = "session_id = ? AND created_at >= ?"
    params: list = [session_id, anchor]
    if until_iso:
        clauses += " AND created_at <= ?"
        params.append(str(until_iso).replace("T", " ")[:19])
    with connect_sessions() as conn:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(total_tokens), 0) as total,
                      COUNT(*) as calls
               FROM token_usage WHERE {clauses}""",
            params,
        ).fetchone()
        return dict(row) if row else {}


def session_last_message_at(session_id: str, after_id: int) -> str | None:
    """created_at of the last message of the TURN opened by message
    `after_id` — the turn-end anchor for turn_metrics.

    "The turn" ends before the next user-role message: a deferred reflect
    grade runs minutes later, by which time the next turn may already have
    rows, and the newest-message shortcut would swallow them.
    """
    with connect_sessions() as conn:
        nxt = conn.execute(
            "SELECT MIN(id) FROM messages WHERE session_id = ? AND id > ? AND role = 'user'",
            (session_id, int(after_id)),
        ).fetchone()
        next_user_id = nxt[0] if nxt else None
        q = "SELECT created_at FROM messages WHERE session_id = ? AND id > ?"
        params: list = [session_id, int(after_id)]
        if next_user_id is not None:
            q += " AND id < ?"
            params.append(int(next_user_id))
        row = conn.execute(q + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return str(row[0]) if row and row[0] else None


def token_usage_by_model_since(since_iso: str) -> list[dict]:
    """Per-model token totals across ALL sessions since a timestamp.

    Feeds the fallback-burn watch (core/llm/burnwatch.py): the fallback
    model's share of a window's tokens is the signature of a wedged
    primary provider silently rerouting every call to the paid tier.
    """
    anchor = str(since_iso).replace("T", " ")[:19]
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT model, provider,
                      COALESCE(SUM(total_tokens), 0) as total,
                      COUNT(*) as calls
               FROM token_usage WHERE created_at >= ?
               GROUP BY model, provider ORDER BY total DESC""",
            (anchor,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


def add_question(
    session_id: str,
    question: str,
    session_title: str = "",
    session_type: str = "normal",
    context: str = "",
    urgency: str = "normal",
    question_type: str = "question",
) -> str:
    qid = _new_id()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO questions (id, session_id, session_title, session_type,
               question, context, urgency, question_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (qid, session_id, session_title, session_type, question, context, urgency, question_type, _now()),
        )
    return qid


def get_questions(session_id: str | None = None) -> list[dict]:
    """Pending (unanswered) questions. Answered rows stay in the table as an
    audit trail until pruned; queue consumers must not see them."""
    with connect_sessions() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM questions WHERE session_id = ? AND answered_at IS NULL ORDER BY created_at",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM questions WHERE answered_at IS NULL ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def delete_question(question_id: str) -> None:
    with connect_sessions() as conn:
        conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def add_notification(
    session_id: str = "",
    title: str = "",
    body: str = "",
    urgency: str = "normal",
    dedup_key: str = "",
) -> str:
    """Insert a notification row.

    `dedup_key` suppresses repeats: the same key inserts at most once per
    UTC day (tracked in snooze_state — no schema change), for idle-loop
    producers that re-derive the same message on a fixed cadence. The FIRST
    notification of a day is byte-identical to the pre-dedup behavior; only
    the repeats are swallowed (returned id is "" then). Empty key = always
    insert (the old behavior).
    """
    if dedup_key:
        marker = f"notify_dedup:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}:{dedup_key}"
        if get_snooze_state(marker):
            return ""
        set_snooze_state(marker, "1")
    nid = _new_id()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO notifications (id, session_id, title, body, urgency, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (nid, session_id, title, body, urgency, _now()),
        )
    return nid


def get_notifications(limit: int = 200) -> list[dict]:
    """Newest first, bounded — the bell renders a list, not an archive."""
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_notification(notification_id: str) -> None:
    with connect_sessions() as conn:
        conn.execute("DELETE FROM notifications WHERE id = ?", (notification_id,))


def prune_notifications(retention_days: int) -> int:
    """Delete notifications older than the retention window. Returns count.

    Until v3.1 nothing pruned this table — it only ever shrank by manual
    dismiss clicks while idle-loop producers refilled it on a cadence.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat()
    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Push subscriptions
# ---------------------------------------------------------------------------


def upsert_push_subscription(endpoint: str, p256dh: str, auth: str) -> str:
    """Insert or update a push subscription (keyed by endpoint). Returns id."""
    sid = _new_id()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO push_subscriptions (id, endpoint, p256dh, auth, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                   p256dh=excluded.p256dh,
                   auth=excluded.auth,
                   created_at=excluded.created_at""",
            (sid, endpoint, p256dh, auth, _now()),
        )
    return sid


def get_push_subscriptions() -> list[dict]:
    with connect_sessions() as conn:
        rows = conn.execute("SELECT * FROM push_subscriptions").fetchall()
        return [dict(r) for r in rows]


def delete_push_subscription(endpoint: str) -> None:
    with connect_sessions() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


# ---------------------------------------------------------------------------
# Session Messages (inter-session)
# ---------------------------------------------------------------------------


def send_session_message(
    sender_id: str,
    recipient_id: str,
    message_type: str,
    payload: str,
) -> None:
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO session_messages (sender_id, recipient_id,
               message_type, payload, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (sender_id, recipient_id, message_type, payload, _now()),
        )


def add_cron_run(
    job_name: str,
    session_id: str | None = None,
    status: str = "running",
    fire_time: str | None = None,
) -> int:
    """Record a cron run. status='claimed' + fire_time implements
    claim-before-deliver: the row exists before the prompt is dispatched, so
    a crash between claim and dispatch is visible (and never replayed) at the
    next startup."""
    with connect_sessions() as conn:
        cur = conn.execute(
            "INSERT INTO cron_runs (job_name, session_id, started_at, status, fire_time) VALUES (?, ?, ?, ?, ?)",
            (job_name, session_id, _now(), status, fire_time),
        )
        return cur.lastrowid


def update_cron_run(run_id: int, status: str, error: str | None = None, session_id: str | None = None) -> None:
    """Advance a cron run's status. session_id, when given, back-fills the
    row — fresh-session jobs claim the run BEFORE the dispatch session
    exists, and without the back-fill the History tab's session link stayed
    NULL forever for every such run."""
    with connect_sessions() as conn:
        sid_sql = ", session_id = ?" if session_id else ""
        sid_arg = [session_id] if session_id else []
        if status in ("claimed", "running"):
            # Non-terminal transition — completed_at stays empty until the
            # run actually finishes.
            conn.execute(
                f"UPDATE cron_runs SET status = ?, error = ?{sid_sql} WHERE id = ?",
                (status, error, *sid_arg, run_id),
            )
        else:
            conn.execute(
                f"UPDATE cron_runs SET status = ?, error = ?, completed_at = ?{sid_sql} WHERE id = ?",
                (status, error, _now(), *sid_arg, run_id),
            )


def reconcile_uncertain_cron_runs() -> list[dict]:
    """Mark runs stuck in claimed/running as 'uncertain' — never replayed.

    Called at startup BEFORE the scheduler initializes. A 'claimed' row means
    the process died between claim and dispatch (the prompt may or may not
    have been sent); a 'running' row means it died mid-run. Either way the
    honest answer is "uncertain": report it, don't guess, don't re-send.
    Returns the affected rows so the caller can notify the user.
    """
    with connect_sessions() as conn:
        rows = conn.execute("SELECT * FROM cron_runs WHERE status IN ('claimed', 'running')").fetchall()
        affected = [dict(r) for r in rows]
        if affected:
            conn.execute(
                "UPDATE cron_runs SET status = 'uncertain', completed_at = ?, "
                "error = 'server restarted mid-run; outcome unknown, not replayed' "
                "WHERE status IN ('claimed', 'running')",
                (_now(),),
            )
        return affected


# ---------------------------------------------------------------------------
# Goals (adaptation plan 3b)
# ---------------------------------------------------------------------------


def create_goal(
    session_id: str,
    objective: str,
    token_budget: int | None = None,
    time_budget_s: int | None = None,
    continuation_budget: int = 0,
) -> int | None:
    """Create a goal. Returns None if the session already has an active one
    (one active goal per session — update or complete it first).

    The check and the insert are one BEGIN IMMEDIATE transaction. Without it
    the SELECT ran in autocommit — Python's sqlite3 in legacy isolation mode
    begins a transaction only before DML, so the read happened outside the
    transaction the INSERT opened, and two concurrent callers (this is
    reachable from an agent tool) both saw "no active goal" and both inserted.
    Same explicit-transaction pattern as the migration runner. The v26 partial
    unique index is the backstop: if a writer on another connection wins the
    race anyway, the INSERT raises IntegrityError and we honour the documented
    contract by returning None rather than surfacing a DB error to the agent.
    """
    with connect_sessions() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT id FROM session_goals WHERE session_id = ? "
                "AND status IN ('active', 'paused', 'budget_limited')",
                (session_id,),
            ).fetchone()
            if existing:
                conn.execute("ROLLBACK")
                return None
            cur = conn.execute(
                "INSERT INTO session_goals (session_id, objective, status, token_budget, time_budget_s, "
                "continuation_budget, started_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
                (session_id, objective[:4000], token_budget, time_budget_s, continuation_budget, _now(), _now()),
            )
            goal_id = cur.lastrowid
            conn.execute("COMMIT")
            return goal_id
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return None
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_active_goal(session_id: str) -> dict | None:
    """The session's live goal (active/paused/budget_limited), if any."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT * FROM session_goals WHERE session_id = ? "
            "AND status IN ('active', 'paused', 'budget_limited') ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None


def update_goal(goal_id: int, **fields) -> None:
    allowed = {"objective", "status", "token_budget", "time_budget_s", "continuation_budget", "continuations_used"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    if fields.get("status") in ("complete", "error"):
        sets.append("completed_at = ?")
        params.append(_now())
    params.append(goal_id)
    with connect_sessions() as conn:
        conn.execute(f"UPDATE session_goals SET {', '.join(sets)} WHERE id = ?", params)


def goal_token_usage(goal_id: int) -> int:
    """Total tokens billed to this goal — flat SUM across all sessions
    (workers stamp the parent's goal_id at write time)."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM token_usage WHERE goal_id = ?",
            (goal_id,),
        ).fetchone()
        return int(row["t"]) if row else 0


def goal_token_usage_since(goal_id: int, since_iso: str) -> int:
    """Windowed goal spend — the telos binding monitor's real-budget input."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM token_usage WHERE goal_id = ? AND created_at >= ?",
            (goal_id, since_iso),
        ).fetchone()
        return int(row["t"]) if row else 0


def total_token_usage_since(since_iso: str) -> int:
    """Windowed total spend across every session and goal."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM token_usage WHERE created_at >= ?",
            (since_iso,),
        ).fetchone()
        return int(row["t"]) if row else 0


def reconcile_orphan_goals() -> int:
    """Goals whose session row no longer exists -> error (startup sweep)."""
    with connect_sessions() as conn:
        cur = conn.execute(
            "UPDATE session_goals SET status = 'error', updated_at = ?, completed_at = ? "
            "WHERE status IN ('active', 'paused', 'budget_limited') "
            "AND session_id NOT IN (SELECT id FROM sessions)",
            (_now(), _now()),
        )
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Gates (adaptation plan 3a)
# ---------------------------------------------------------------------------


def add_gate(
    session_id: str,
    name: str,
    command: str,
    watch_paths: list[str] | None = None,
    cwd: str | None = None,
    scope: str = "session",
) -> int:
    """Create or replace a gate (upsert on (session_id, name))."""
    import json as _json

    with connect_sessions() as conn:
        cur = conn.execute(
            "INSERT INTO gates (session_id, scope, name, command, watch_paths, cwd, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(session_id, name) DO UPDATE SET "
            "command = excluded.command, watch_paths = excluded.watch_paths, "
            "cwd = excluded.cwd, scope = excluded.scope, enabled = 1",
            (session_id, scope, name, command, _json.dumps(watch_paths or []), cwd, _now()),
        )
        return cur.lastrowid


def get_gates(session_id: str, enabled_only: bool = True) -> list[dict]:
    import json as _json

    with connect_sessions() as conn:
        q = "SELECT * FROM gates WHERE session_id = ?"
        if enabled_only:
            q += " AND enabled = 1"
        rows = conn.execute(q + " ORDER BY id", (session_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["watch_paths"] = _json.loads(d.get("watch_paths") or "[]")
            except (ValueError, TypeError):
                d["watch_paths"] = []
            out.append(d)
        return out


def remove_gate(session_id: str, name: str) -> bool:
    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM gates WHERE session_id = ? AND name = ?", (session_id, name))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Canary runs (adaptation plan 3.5): active-measurement scoring rows
# ---------------------------------------------------------------------------


def add_canary_run(
    task: str,
    trigger: str,
    session_id: str | None,
    gate_results_json: str,
    passed: bool,
    retries: int = 0,
    tokens: int = 0,
    duration_s: float = 0.0,
    batch_id: str | None = None,
    outcome: str = "",
    error: str = "",
) -> int:
    """Record a completed canary run. batch_id links post-batch sweeps to the
    Phase 4 adaptive batch that triggered them (the tripwire joins on it).
    outcome separates timeout/error/noop from honest gate failures; rows
    written before v30 keep it NULL."""
    with connect_sessions() as conn:
        cur = conn.execute(
            """INSERT INTO canary_runs
               (task, trigger, batch_id, session_id, gate_results_json,
                passed, retries, tokens, duration_s, outcome, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task,
                trigger,
                batch_id,
                session_id,
                gate_results_json,
                1 if passed else 0,
                int(retries),
                int(tokens),
                float(duration_s),
                outcome or None,
                error or None,
                _now(),
            ),
        )
        return cur.lastrowid


def list_canary_runs(
    task: str | None = None,
    batch_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Canary runs, newest first, optionally filtered by task or batch."""
    clauses = []
    params: list = []
    if task:
        clauses.append("task = ?")
        params.append(task)
    if batch_id:
        clauses.append("batch_id = ?")
        params.append(batch_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    with connect_sessions() as conn:
        rows = conn.execute(
            f"SELECT * FROM canary_runs {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def prune_canary_runs(retention_days: int) -> int:
    """Delete canary runs older than the retention window. Returns count."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat()
    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM canary_runs WHERE created_at < ?", (cutoff,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Adaptive layer (adaptation plan 4a): entries, events, batches, proposals
# ---------------------------------------------------------------------------


def adaptive_get_entry(entry_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM adaptive_entries WHERE id = ?", (entry_id,)).fetchone()
        return dict(row) if row else None


# Statuses an entry can hold and still be part of the live prompt population:
# `active`, and `trial` (W6 — a trial entry renders on half the turns, counts
# against the per-kind cap, and is updated and retired like any other). Pass
# it as `status=` wherever "what is currently in play" is the question.
ADAPTIVE_LIVE_STATUS = "active,trial"


def adaptive_list_entries(
    kind: str | None = None,
    scope: str | None = None,
    status: str | None = "active",
    limit: int = 200,
) -> list[dict]:
    """Entries by kind/scope/status, ordered (kind, id).

    `status` takes one status, a comma-separated list of them
    (`ADAPTIVE_LIVE_STATUS`), or a sequence; None means every status.
    """
    clauses = []
    params: list = []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    if status:
        wanted = (
            [s.strip() for s in status.split(",") if s.strip()]
            if isinstance(status, str)
            else [str(s).strip() for s in status if str(s).strip()]
        )
        if len(wanted) == 1:
            clauses.append("status = ?")
            params.append(wanted[0])
        elif wanted:
            clauses.append(f"status IN ({', '.join('?' * len(wanted))})")
            params.extend(wanted)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    with connect_sessions() as conn:
        rows = conn.execute(
            f"SELECT * FROM adaptive_entries {where} ORDER BY kind, id LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def adaptive_put_entry(row: dict) -> None:
    """Write a full entry row (insert or replace). The apply/rollback engine
    owns version arithmetic — this is a dumb store."""
    with connect_sessions() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO adaptive_entries
               (id, kind, scope, title, content, risk, version, status, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["kind"],
                row.get("scope", "global"),
                row.get("title", ""),
                row.get("content", ""),
                row.get("risk", "low"),
                int(row.get("version", 1)),
                row.get("status", "active"),
                row.get("source", "user"),
                row.get("created_at") or _now(),
                row.get("updated_at") or _now(),
            ),
        )


def adaptive_remove_entry(entry_id: str) -> None:
    """Hard delete — used only by rollback of a create (before_json absent)."""
    with connect_sessions() as conn:
        conn.execute("DELETE FROM adaptive_entries WHERE id = ?", (entry_id,))


def adaptive_entry_count(kind: str) -> int:
    """Live entries of this kind — the number the per-kind cap is checked
    against. Trial entries count: they render (on half the turns), they hold a
    slot, and leaving them out would let the cap be bypassed entirely by
    turning trial mode on."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM adaptive_entries WHERE kind = ? AND status IN ('active', 'trial')",
            (kind,),
        ).fetchone()
        return int(row["n"])


def adaptive_add_event(
    entry_id: str,
    action: str,
    before_json: str | None,
    after_json: str | None,
    evidence_json: str,
    actor: str,
    batch_id: str | None = None,
    proposal_id: int | None = None,
) -> int:
    with connect_sessions() as conn:
        cur = conn.execute(
            """INSERT INTO adaptive_events
               (entry_id, action, before_json, after_json, evidence_json,
                actor, proposal_id, batch_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry_id, action, before_json, after_json, evidence_json, actor, proposal_id, batch_id, _now()),
        )
        return cur.lastrowid


def adaptive_list_events(
    batch_id: str | None = None,
    entry_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Events newest-first (UI order). Rollback uses events_for_batch."""
    clauses = []
    params: list = []
    if batch_id:
        clauses.append("batch_id = ?")
        params.append(batch_id)
    if entry_id:
        clauses.append("entry_id = ?")
        params.append(entry_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    with connect_sessions() as conn:
        rows = conn.execute(
            f"SELECT * FROM adaptive_events {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def adaptive_events_for_batch(batch_id: str) -> list[dict]:
    """Ascending autoincrement order — reverse it to roll back."""
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM adaptive_events WHERE batch_id = ? ORDER BY id ASC",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def adaptive_get_event(event_id: int) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM adaptive_events WHERE id = ?", (int(event_id),)).fetchone()
        return dict(row) if row else None


def adaptive_auto_apply_batches_since(since_iso: str) -> int:
    """Distinct batches auto-applied since the cutoff (daily-cap accounting)."""
    with connect_sessions() as conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT batch_id) AS n FROM adaptive_events
               WHERE actor = 'auto' AND action != 'rollback' AND created_at >= ?""",
            (since_iso,),
        ).fetchone()
        return int(row["n"])


def adaptive_create_batch(batch_id: str, producer: str, payload_json: str, status: str = "pending") -> None:
    with connect_sessions() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO adaptive_batches
               (batch_id, producer, status, payload_json, flagged_reason, cleared_at, created_at)
               VALUES (?, ?, ?, ?, NULL, NULL, ?)""",
            (batch_id, producer, status, payload_json, _now()),
        )


def adaptive_get_batch(batch_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM adaptive_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        return dict(row) if row else None


def adaptive_list_batches(status: str | None = None, limit: int = 100) -> list[dict]:
    with connect_sessions() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM adaptive_batches WHERE status = ? ORDER BY created_at ASC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM adaptive_batches ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]


def adaptive_update_batch(
    batch_id: str,
    status: str | None = None,
    flagged_reason: str | None = None,
    cleared_at: str | None = None,
    payload_json: str | None = None,
) -> None:
    updates: dict = {}
    if status is not None:
        updates["status"] = status
    if flagged_reason is not None:
        updates["flagged_reason"] = flagged_reason
    if cleared_at is not None:
        updates["cleared_at"] = cleared_at
    if payload_json is not None:
        updates["payload_json"] = payload_json
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    with connect_sessions() as conn:
        conn.execute(f"UPDATE adaptive_batches SET {cols} WHERE batch_id = ?", [*updates.values(), batch_id])


# Producers re-derive and re-offer the same findings every cycle while the
# review queue stays full, so an unconditional WARNING per refusal compounds
# into thousands of identical lines (~32% of a day's log on the live box while
# dream's share sat at cap). Warn once per producer per fill episode; a
# successful insert re-arms the warning. Process-lifetime state is fine here —
# the point is log volume, not exact bookkeeping across restarts.
_refusal_warned: set[str] = set()


def _log_refusal(producer: str, message: str, *args) -> None:
    if producer in _refusal_warned:
        logger.debug(message, *args)
    else:
        _refusal_warned.add(producer)
        logger.warning(message + " (further refusals for this producer log at DEBUG)", *args)


def adaptive_add_proposal(
    producer: str,
    payload_json: str,
    evidence_json: str,
    rationale: str,
    max_pending: int = 0,
    max_pending_per_producer: int = 0,
) -> int | None:
    """Queue a proposal for human review. Returns the id, or None if suppressed.

    Two bounds, because this queue is written by machines and drained by a
    person. Producers emit continuously and approval is a scarce human act,
    so without them the backlog only ever grows — 126 pending on the live
    box, untouched for five days, which is a queue nobody finishes reading.

    * **Dedupe** — an identical pending payload from the same producer
      returns the existing id rather than stacking a copy. Re-deriving the
      same finding from the same evidence is normal producer behaviour, not
      new information.
    * **Cap** — at `max_pending` the insert is refused. Refusing is the
      honest response: another row on a queue this long would not be
      reviewed either, and a caller that hears "no" can say so.
    * **Per-producer share** — `max_pending_per_producer` stops one
      chatty producer from owning the whole queue. On the live box every
      one of the 126 backed-up proposals came from `dream`, so once it
      filled the queue Candor, Refine and Telos were refused too: the
      loudest producer silenced the quieter ones, which is the opposite of
      how a review queue should triage.
    """
    with connect_sessions() as conn:
        # BEGIN IMMEDIATE: the dedupe probe and both cap counts below are
        # read-then-write. In autocommit they ran outside the INSERT's own
        # transaction, so a producer on an executor thread and the snooze
        # drain on the loop could both pass the same check and land a
        # duplicate proposal (or overshoot the cap). Same fix v26 applied to
        # create_goal.
        conn.execute("BEGIN IMMEDIATE")
        dup = conn.execute(
            "SELECT id FROM adaptive_proposals WHERE status = 'pending' AND producer = ? AND payload_json = ? "
            "ORDER BY id LIMIT 1",
            (producer, payload_json),
        ).fetchone()
        if dup:
            return int(dup[0])
        if max_pending > 0:
            pending = conn.execute("SELECT COUNT(*) FROM adaptive_proposals WHERE status = 'pending'").fetchone()[0]
            if int(pending) >= max_pending:
                _log_refusal(
                    producer,
                    "adaptive: proposal from %s refused — %d pending at cap %d",
                    producer,
                    pending,
                    max_pending,
                )
                return None
        if max_pending_per_producer > 0:
            mine = conn.execute(
                "SELECT COUNT(*) FROM adaptive_proposals WHERE status = 'pending' AND producer = ?", (producer,)
            ).fetchone()[0]
            if int(mine) >= max_pending_per_producer:
                _log_refusal(
                    producer,
                    "adaptive: proposal from %s refused — %d of its own pending at per-producer cap %d",
                    producer,
                    mine,
                    max_pending_per_producer,
                )
                return None
        cur = conn.execute(
            """INSERT INTO adaptive_proposals
               (producer, payload_json, evidence_json, rationale, status, resolved_at, created_at)
               VALUES (?, ?, ?, ?, 'pending', NULL, ?)""",
            (producer, payload_json, evidence_json, rationale, _now()),
        )
        _refusal_warned.discard(producer)  # queue has room again — re-arm the refusal warning
        return cur.lastrowid


def adaptive_count_pending_proposals(producer: str | None = None) -> int:
    """Pending proposals overall, or just one producer's (the per-producer
    share in adaptive_add_proposal is checked against the latter)."""
    with connect_sessions() as conn:
        if producer:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM adaptive_proposals WHERE status = 'pending' AND producer = ?",
                    (producer,),
                ).fetchone()[0]
            )
        return int(conn.execute("SELECT COUNT(*) FROM adaptive_proposals WHERE status = 'pending'").fetchone()[0])


def adaptive_count_auto_approved_since(since_iso: str) -> int:
    """Auto-approvals in a window — the daily budget check for the veto-window
    drain. Counts by the distinct 'auto_approved' status, which is exactly why
    that status exists instead of reusing 'approved'."""
    with connect_sessions() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM adaptive_proposals WHERE status = 'auto_approved' AND resolved_at >= ?",
                (since_iso,),
            ).fetchone()[0]
        )


def adaptive_expire_stale_proposals(max_age_days: int) -> int:
    """Expire pending proposals past the TTL. Returns the number expired.

    A proposal is a snapshot of evidence at a moment. Weeks later the entries
    it cites may have moved, the tool it complains about may have recovered,
    and approving it blind is worse than letting it lapse — the producer will
    re-raise it from current evidence if it still holds.
    """
    if max_age_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with connect_sessions() as conn:
        cur = conn.execute(
            "UPDATE adaptive_proposals SET status = 'expired', resolved_at = ? "
            "WHERE status = 'pending' AND created_at < ?",
            (_now(), cutoff),
        )
        return cur.rowcount


def adaptive_get_proposal(proposal_id: int) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM adaptive_proposals WHERE id = ?", (int(proposal_id),)).fetchone()
        return dict(row) if row else None


def adaptive_list_proposals(status: str | None = None, limit: int = 100) -> list[dict]:
    with connect_sessions() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM adaptive_proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM adaptive_proposals ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]


def adaptive_resolve_proposal(proposal_id: int, status: str) -> None:
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE adaptive_proposals SET status = ?, resolved_at = ? WHERE id = ?",
            (status, _now(), int(proposal_id)),
        )


def adaptive_annotate_proposal(proposal_id: int, suffix: str) -> None:
    """Append an audit note to a proposal's rationale (idempotent per text)."""
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE adaptive_proposals SET rationale = rationale || ? WHERE id = ? AND rationale NOT LIKE ?",
            (suffix, int(proposal_id), f"%{suffix}"),
        )


def list_cron_runs(job_name: str | None = None, limit: int = 50) -> list[dict]:
    with connect_sessions() as conn:
        if job_name:
            rows = conn.execute(
                "SELECT * FROM cron_runs WHERE job_name = ? ORDER BY started_at DESC LIMIT ?",
                (job_name, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cron_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def list_cron_runs_paginated(
    limit: int = 50,
    offset: int = 0,
    job_name: str | None = None,
) -> tuple[list[dict], int]:
    """Paginated cron run listing. Returns (rows, total_count)."""
    with connect_sessions() as conn:
        if job_name:
            total = conn.execute("SELECT COUNT(*) as cnt FROM cron_runs WHERE job_name = ?", (job_name,)).fetchone()[
                "cnt"
            ]
            rows = conn.execute(
                "SELECT * FROM cron_runs WHERE job_name = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (job_name, limit, offset),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) as cnt FROM cron_runs").fetchone()["cnt"]
            rows = conn.execute(
                "SELECT * FROM cron_runs ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows], total


def get_cron_run_stats(job_name: str) -> dict:
    """Get run count and last_run_at for a job."""
    with connect_sessions() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as run_count, MAX(started_at) as last_run_at
               FROM cron_runs WHERE job_name = ?""",
            (job_name,),
        ).fetchone()
        return dict(row) if row else {"run_count": 0, "last_run_at": None}


def prune_cron_runs(max_age_days: int = 30, keep_per_job: int = 100) -> int:
    """Delete old cron run records. Returns count deleted."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with connect_sessions() as conn:
        cur = conn.execute(
            """DELETE FROM cron_runs
               WHERE started_at < ? AND id NOT IN (
                   SELECT id FROM cron_runs cr2
                   WHERE cr2.job_name = cron_runs.job_name
                   ORDER BY cr2.started_at DESC LIMIT ?
               )""",
            (cutoff, keep_per_job),
        )
        return cur.rowcount


def clear_cron_runs(job_name: str | None = None) -> int:
    """Delete completed/error cron run records. Preserves running jobs. Returns count deleted."""
    with connect_sessions() as conn:
        if job_name:
            cur = conn.execute(
                "DELETE FROM cron_runs WHERE job_name = ? AND status NOT IN ('running')",
                (job_name,),
            )
        else:
            cur = conn.execute(
                "DELETE FROM cron_runs WHERE status NOT IN ('running')",
            )
        return cur.rowcount


def list_session_ids_by_type_before(session_type: str, cutoff_iso: str) -> list[str]:
    """Ids of every session of one type not updated since the cutoff — a
    direct query, oldest first. The retention sweeps used to walk
    list_sessions(500), the 500 most RECENTLY updated rows, so once the
    table passed 500 the oldest sessions — the ones due for pruning — were
    the ones the sweep could not see."""
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE session_type = ? AND updated_at < ? ORDER BY updated_at",
            (session_type, cutoff_iso),
        ).fetchall()
        return [r["id"] for r in rows]


def watched_worker_ids() -> set[str]:
    """Worker ids some parent is still waiting on (state awaiting_workers)."""
    out: set[str] = set()
    with connect_sessions() as conn:
        rows = conn.execute("SELECT watched_worker_ids FROM sessions WHERE state_v2 = 'awaiting_workers'").fetchall()
    for r in rows:
        try:
            out.update(str(x) for x in json.loads(r["watched_worker_ids"] or "[]"))
        except (TypeError, ValueError):
            continue
    return out


def delete_old_dream_hypotheses(cutoff_iso: str, statuses: tuple[str, ...]) -> int:
    """Delete hypotheses in the given (terminal) statuses created before the
    cutoff. Returns the row count."""
    if not statuses:
        return 0
    placeholders = ",".join("?" * len(statuses))
    with connect_sessions() as conn:
        cur = conn.execute(
            f"DELETE FROM dream_hypotheses WHERE status IN ({placeholders}) AND created_at < ?",
            (*statuses, cutoff_iso),
        )
        return cur.rowcount


def list_cron_sessions_before(max_age_days: int = 7) -> list[dict]:
    """Cron sessions the pruner would delete — the single criteria
    definition, shared with retention's distill-before-delete digest.

    Keyed on session_type = 'cron', the column the scheduler stamps on every
    session it creates — never on the title. The title is a display string
    the user and the LLM titler both control: the old `title LIKE 'Cron: %'`
    sweep cascade-deleted a normal session someone had renamed "Cron: …"
    after seven idle days, while the scheduler's own "Job test: …" sessions
    (also type cron) never matched and accumulated forever. Workers spawned
    by a cron run are type 'worker' and go with their parent via
    delete_session's cascade, so no parent clause is needed.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with connect_sessions() as conn:
        # pinned exclusion: a user who pins a cron session is saying "keep
        # this run" — the sweep must not eat it.
        rows = conn.execute(
            "SELECT id, title, updated_at FROM sessions "
            "WHERE session_type = 'cron' AND updated_at < ? AND COALESCE(pinned, 0) = 0",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def prune_cron_sessions(max_age_days: int = 7) -> int:
    """Delete old auto-created cron sessions and their messages."""
    rows = list_cron_sessions_before(max_age_days)
    for r in rows:
        delete_session(r["id"])
    return len(rows)


# ---------------------------------------------------------------------------
# Schema Meta
# ---------------------------------------------------------------------------


def get_schema_version() -> int:
    with connect_sessions() as conn:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0


# ---------------------------------------------------------------------------
# Maintenance helpers
# ---------------------------------------------------------------------------


def cleanup_old_partials(max_age_hours: int = 1) -> int:
    """Delete partial messages older than max_age_hours. Returns count deleted."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM messages WHERE partial = 1 AND created_at < ?", (cutoff,))
        return cur.rowcount


def prune_orphaned_token_usage(max_age_days: int = 30) -> int:
    """Delete token_usage rows with NULL session_id or older than max_age_days."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with connect_sessions() as conn:
        # Orphaned rows (session deleted, FK set to NULL). Rows billed to a
        # LIVE goal are exempt — a long-lived goal must not lose its
        # accounting base to the 30-day TTL (plan 3b).
        live_goal = "(goal_id IS NULL OR goal_id NOT IN (SELECT id FROM session_goals WHERE status IN ('active', 'paused', 'budget_limited')))"
        cur1 = conn.execute(f"DELETE FROM token_usage WHERE session_id IS NULL AND {live_goal}")
        # Old rows beyond retention
        cur2 = conn.execute(f"DELETE FROM token_usage WHERE created_at < ? AND {live_goal}", (cutoff,))
        total = (cur1.rowcount or 0) + (cur2.rowcount or 0)
        return total


def prune_old_session_messages(max_age_days: int = 7) -> int:
    """Delete read session_messages older than max_age_days."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with connect_sessions() as conn:
        cur = conn.execute(
            "DELETE FROM session_messages WHERE read_at IS NOT NULL AND created_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0


def prune_old_questions(max_age_days: int = 7) -> int:
    """Delete questions older than max_age_days."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM questions WHERE created_at < ?", (cutoff,))
        return cur.rowcount or 0


def incremental_vacuum(pages: int = 100) -> None:
    if not isinstance(pages, int) or pages < 0 or pages > 10000:
        raise ValueError(f"Invalid vacuum pages value: {pages}")
    with connect_sessions() as conn:
        conn.execute(f"PRAGMA incremental_vacuum({pages})")


def checkpoint() -> None:
    with connect_sessions() as conn:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")


# ---------------------------------------------------------------------------
# Snooze
# ---------------------------------------------------------------------------


def get_unreviewed_sessions(min_age_minutes: int = 10, limit: int = 5) -> list[dict]:
    """Get sessions eligible for Snooze catch-up distillation.

    Archived sessions are out: archiving is the user saying this
    conversation is finished, and spending a distillation call on it burns
    budget the live backlog wants."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)).isoformat()
    with connect_sessions() as conn:
        rows = conn.execute(
            f"""SELECT s.* FROM sessions s
               WHERE s.snooze_reviewed_at IS NULL
                 AND {SQL_SESSION_IS_IDLE}
                 AND s.updated_at < ?
                 AND s.archived_at IS NULL
                 AND s.session_type NOT IN ('worker', 'canary')
                 AND (
                     SELECT COUNT(*) FROM messages m
                     WHERE m.session_id = s.id
                       AND m.role IN ('user', 'assistant')
                       AND m.content != ''
                 ) >= 4
                 AND (
                     SELECT COALESCE(SUM(m.char_count), 0) FROM messages m
                     WHERE m.session_id = s.id
                       AND m.role IN ('user', 'assistant')
                 ) >= 500
               ORDER BY s.updated_at ASC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_session_reviewed(session_id: str) -> None:
    """Mark a session as reviewed by Snooze."""
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE sessions SET snooze_reviewed_at = ? WHERE id = ?",
            (_now(), session_id),
        )


def get_unrefined_sessions(min_idle_minutes: int = 10, limit: int = 1) -> list[dict]:
    """Sessions eligible for the snooze tail-end refine pass.

    Broader gate than the removed Activity-2b selector: no reflect verdict
    required. Refine looks at the whole transcript, so a smooth-running
    session (no reflect) or a 'pass with no deviation' session still
    qualifies.

    The watermark in ``snooze_state`` under ``refined:{session_id}`` stores
    the MAX message id refine saw (as text). A session becomes eligible
    again once it grows past that id — refine used to get exactly one shot
    per session, which it routinely spent while the session sat parked on an
    unanswered ask_user, hours before the interesting resolution existed
    (session 83dc931a8596 is the type specimen). Migration v32 converted the
    legacy ISO-timestamp watermarks to "processed up to now" so the fleet
    didn't re-refine its entire history on deploy.

    ``awaiting_user`` / ``awaiting_workers`` are deliberately NOT idle here,
    unlike SQL_SESSION_IS_IDLE: those are mid-task pauses, and grading half
    a story wastes refine's one call per cycle. The re-arm makes the wait
    safe — the session qualifies once it finishes and grows.

    Each returned row carries ``refine_max_message_id`` — the value the
    caller must stamp into the watermark after processing, so messages that
    arrive mid-refine aren't silently skipped.
    """
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=min_idle_minutes)).isoformat()
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT s.*,
                      (SELECT COALESCE(MAX(m.id), 0) FROM messages m
                       WHERE m.session_id = s.id) AS refine_max_message_id
               FROM sessions s
               WHERE (s.state_v2 IS NULL OR s.state_v2 IN
                      ('idle_ready', 'cancelling', 'finalizing'))
                 AND s.updated_at < ?
                 AND s.archived_at IS NULL
                 AND s.session_type NOT IN ('worker', 'canary')
                 AND NOT EXISTS (
                     SELECT 1 FROM snooze_state ss
                     WHERE ss.key = 'refined:' || s.id
                       AND CAST(ss.value AS INTEGER) >=
                           (SELECT COALESCE(MAX(m.id), 0) FROM messages m
                            WHERE m.session_id = s.id)
                 )
                 AND EXISTS (
                     SELECT 1 FROM messages m
                     WHERE m.session_id = s.id
                       AND m.role = 'user'
                       AND m.content != ''
                 )
                 AND EXISTS (
                     SELECT 1 FROM messages m
                     WHERE m.session_id = s.id
                       AND m.role = 'assistant'
                       AND m.content != ''
                 )
               ORDER BY s.updated_at ASC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_snooze_state(key: str) -> str | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT value FROM snooze_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_snooze_state(key: str, value: str) -> None:
    with connect_sessions() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snooze_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, _now()),
        )


# ---------------------------------------------------------------------------
# Post-mortems (Phase 2c): reflect-as-compiler artifact stream
# ---------------------------------------------------------------------------


def add_post_mortem(
    session_id: str,
    attempt: int,
    verdict: str,
    failure_cause: str,
    confidence: float,
    reflect_model: str,
    reflect_latency_ms: int,
    scout_viability: str | None,
    execution_mode: str | None,
    payload_json: str,
    outcome_source: str = "llm",
) -> str:
    """Insert a post-mortem row and return its id.

    payload_json carries the full ReflectResult + scout-report summary so
    snooze can re-derive anything the indexed columns don't expose.

    outcome_source is an indexed column as well as a payload key: the whole
    point of it is to be countable ("what share of our outcomes is anything
    better than the grader's own opinion?"), and that question should not
    require parsing every payload in the table.
    """
    pm_id = _new_id()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO post_mortems (
                id, session_id, created_at, attempt, verdict, failure_cause,
                confidence, reflect_model, reflect_latency_ms,
                scout_viability, execution_mode, payload_json, outcome_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pm_id,
                session_id,
                _now(),
                attempt,
                verdict,
                failure_cause,
                float(confidence),
                reflect_model,
                reflect_latency_ms,
                scout_viability,
                execution_mode,
                payload_json,
                outcome_source or "llm",
            ),
        )
    return pm_id


def list_post_mortems(
    session_id: str | None = None, failure_cause: str | None = None, since_iso: str | None = None, limit: int = 100
) -> list[dict]:
    """Query post-mortems with optional filters. Newest first."""
    clauses = []
    params: list = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if failure_cause:
        clauses.append("failure_cause = ?")
        params.append(failure_cause)
    if since_iso:
        clauses.append("created_at >= ?")
        params.append(since_iso)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    with connect_sessions() as conn:
        rows = conn.execute(
            f"""SELECT * FROM post_mortems {where}
               ORDER BY created_at DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_post_mortem(pm_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT * FROM post_mortems WHERE id = ?",
            (pm_id,),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Ground truth (2026-09-04): the user's own verdict on a turn
# ---------------------------------------------------------------------------


def _mid(message_id) -> str:
    """Canonical string form of a message id.

    message_feedback.message_id is TEXT (the API addresses messages by path
    segment) while messages.id is an INTEGER, so "7" and 7 must not become two
    different rows.
    """
    try:
        return str(int(message_id))
    except (TypeError, ValueError):
        return str(message_id)


def upsert_message_feedback(session_id: str, message_id, signal: str, note: str = "") -> dict:
    """Record (or replace) the user's reaction to one assistant message."""
    now = _now()
    mid = _mid(message_id)
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO message_feedback (session_id, message_id, signal, note, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(message_id) DO UPDATE SET
                   session_id = excluded.session_id,
                   signal = excluded.signal,
                   note = excluded.note,
                   created_at = excluded.created_at""",
            (session_id, mid, signal, note or "", now),
        )
    return {"message_id": mid, "signal": signal, "note": note or "", "created_at": now}


def delete_message_feedback(session_id: str, message_id) -> bool:
    """Remove a reaction. True when a row was actually there."""
    with connect_sessions() as conn:
        cur = conn.execute(
            "DELETE FROM message_feedback WHERE session_id = ? AND message_id = ?",
            (session_id, _mid(message_id)),
        )
    return cur.rowcount > 0


def get_message_feedback(session_id: str, message_id) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT * FROM message_feedback WHERE session_id = ? AND message_id = ?",
            (session_id, _mid(message_id)),
        ).fetchone()
    return dict(row) if row else None


def list_message_feedback(session_id: str) -> list[dict]:
    """Every reaction in the session, oldest first — one page for the client."""
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT message_id, signal, note, created_at FROM message_feedback
               WHERE session_id = ? ORDER BY id""",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def turn_user_msg_id_for_message(session_id: str, message_id) -> int | None:
    """The id of the user message that opened the turn this message belongs to.

    Assistant and tool rows carry `metadata.parent_user_msg_id`, stamped by
    run_agent's _save_turn_msg; a user row is its own turn anchor.
    """
    row = get_message(int(message_id)) if str(message_id).lstrip("-").isdigit() else None
    if not row or row.get("session_id") != session_id:
        return None
    if row.get("role") == "user":
        return int(row["id"])
    try:
        meta = json.loads(row.get("metadata") or "{}")
        parent = meta.get("parent_user_msg_id")
        return int(parent) if parent is not None else None
    except (ValueError, TypeError):
        return None


def latest_post_mortem_for_turn(session_id: str, turn_user_msg_id: int) -> dict | None:
    """The post-mortem for one turn — its last attempt, if it was graded.

    New rows carry the turn anchor in their payload. Rows written before
    2026-09-04 don't, so they fall back to the window between this turn's user
    message and the next one; that is exact for synchronous grades and for
    deferred grades of turns nobody replied to, which is every pre-existing
    row worth matching.
    """
    with connect_sessions() as conn:
        row = conn.execute(
            """SELECT * FROM post_mortems
               WHERE session_id = ? AND json_extract(payload_json, '$.turn_user_msg_id') = ?
               ORDER BY attempt DESC, created_at DESC LIMIT 1""",
            (session_id, int(turn_user_msg_id)),
        ).fetchone()
        if row:
            return dict(row)
        anchor = conn.execute(
            "SELECT created_at FROM messages WHERE id = ? AND session_id = ?",
            (int(turn_user_msg_id), session_id),
        ).fetchone()
        if not anchor:
            return None
        nxt = conn.execute(
            """SELECT created_at FROM messages
               WHERE session_id = ? AND role = 'user' AND id > ?
               ORDER BY id LIMIT 1""",
            (session_id, int(turn_user_msg_id)),
        ).fetchone()
        if nxt:
            row = conn.execute(
                """SELECT * FROM post_mortems
                   WHERE session_id = ? AND created_at >= ? AND created_at < ?
                   ORDER BY attempt DESC, created_at DESC LIMIT 1""",
                (session_id, anchor["created_at"], nxt["created_at"]),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM post_mortems
                   WHERE session_id = ? AND created_at >= ?
                   ORDER BY attempt DESC, created_at DESC LIMIT 1""",
                (session_id, anchor["created_at"]),
            ).fetchone()
    return dict(row) if row else None


def set_post_mortem_user_signal(session_id: str, message_id, signal: str | None) -> dict | None:
    """Stamp a thumb onto the post-mortem of the turn `message_id` belongs to.

    Returns the post-mortem row as it stood BEFORE the write (the caller needs
    its payload to know which entries the turn cited, and what it had already
    applied), or None when the turn was never graded — a thumb on an ungraded
    turn is still recorded as feedback, it just has no verdict to contradict.

    Clearing the signal restores the outcome_source the grade itself produced,
    so removing a thumb is a real undo rather than a permanent 'user' stamp.
    """
    turn_id = turn_user_msg_id_for_message(session_id, message_id)
    if turn_id is None:
        return None
    pm = latest_post_mortem_for_turn(session_id, turn_id)
    if not pm:
        return None
    if signal:
        source = "user"
    else:
        try:
            source = (json.loads(pm.get("payload_json") or "{}") or {}).get("outcome_source") or "llm"
        except (ValueError, TypeError):
            source = "llm"
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE post_mortems SET user_signal = ?, outcome_source = ? WHERE id = ?",
            (signal, source, pm["id"]),
        )
    return pm


def update_post_mortem_payload(pm_id: str, payload: dict) -> None:
    """Replace a post-mortem's payload. Used to record what a thumb applied."""
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE post_mortems SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), pm_id),
        )


# ---------------------------------------------------------------------------
# Trust surface counters (/api/trust)
#
# Every one of these answers with zeros rather than raising: the endpoint is a
# dashboard, and a table another workstream has not created yet must read as
# "nothing recorded", never as a 500.
# ---------------------------------------------------------------------------


def post_mortem_outcome_counts(since_iso: str) -> dict:
    """Graded turns in the window, split by what their outcome rests on."""
    counts = {"llm": 0, "next_turn": 0, "user": 0}
    total = 0
    try:
        with connect_sessions() as conn:
            rows = conn.execute(
                """SELECT COALESCE(outcome_source, 'llm') AS src, COUNT(*) AS n
                   FROM post_mortems WHERE created_at >= ? GROUP BY src""",
                (since_iso,),
            ).fetchall()
    except sqlite3.Error as e:
        logger.debug("outcome-source counts unavailable: %s", e)
        return {"by_source": counts, "graded": 0}
    for r in rows:
        total += int(r["n"])
        if r["src"] in counts:
            counts[r["src"]] = int(r["n"])
    return {"by_source": counts, "graded": total}


def count_user_turns_since(since_iso: str) -> int:
    """User messages in the window — the denominator "how many turns were there".

    Harness-authored user rows (the worker-resume injection) are excluded: they
    are the system talking to itself, and counting them would understate the
    share of real turns that got graded.
    """
    try:
        with connect_sessions() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM messages
                   WHERE role = 'user' AND created_at >= ?
                     AND COALESCE(content, '') NOT LIKE '[Watched workers have completed%'""",
                (since_iso,),
            ).fetchone()
        return int(row["n"]) if row else 0
    except sqlite3.Error as e:
        logger.debug("user-turn count unavailable: %s", e)
        return 0


def post_mortems_with_user_signal(since_iso: str | None = None, limit: int = 1000) -> list[dict]:
    """Graded turns the user reacted to — the grader's own report card."""
    try:
        with connect_sessions() as conn:
            if since_iso:
                rows = conn.execute(
                    """SELECT verdict, user_signal FROM post_mortems
                       WHERE user_signal IS NOT NULL AND created_at >= ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (since_iso, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT verdict, user_signal FROM post_mortems
                       WHERE user_signal IS NOT NULL
                       ORDER BY created_at DESC LIMIT ?""",
                    (int(limit),),
                ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logger.debug("user-signal post-mortems unavailable: %s", e)
        return []


def adaptive_entry_status_counts() -> dict:
    """How many adaptive entries sit in each status."""
    try:
        with connect_sessions() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM adaptive_entries GROUP BY status").fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}
    except sqlite3.Error as e:
        logger.debug("adaptive status counts unavailable: %s", e)
        return {}


def canary_outcome_counts(since_iso: str) -> dict:
    """Canary runs in the window: how many ran, failed, and were contaminated.

    `contaminated` is written by the post-run contamination scan; until that
    ships the count is honestly zero rather than absent.
    """
    zero = {"runs": 0, "fails": 0, "contaminated": 0}
    try:
        with connect_sessions() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS runs,
                          SUM(CASE WHEN COALESCE(passed, 0) = 0 THEN 1 ELSE 0 END) AS fails,
                          SUM(CASE WHEN outcome = 'contaminated' THEN 1 ELSE 0 END) AS contaminated
                   FROM canary_runs WHERE created_at >= ?""",
                (since_iso,),
            ).fetchone()
    except sqlite3.Error as e:
        logger.debug("canary outcome counts unavailable: %s", e)
        return zero
    if not row:
        return zero
    return {
        "runs": int(row["runs"] or 0),
        "fails": int(row["fails"] or 0),
        "contaminated": int(row["contaminated"] or 0),
    }


def list_unsynthesized_post_mortems(limit: int = 500) -> list[dict]:
    """Return post-mortems not yet processed by snooze synthesis, oldest first."""
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT * FROM post_mortems WHERE synthesized_at IS NULL
               ORDER BY created_at ASC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def search_post_mortems_for_scout(
    failure_cause: str | None = None,
    subject: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Scout-facing search over post-mortems for on-demand failure lookup.

    Filters:
      - failure_cause: exact match on the indexed column
      - subject: substring match against payload_json (tool/skill name etc.)

    Both filters optional. Returns newest first, capped at 10.
    """
    limit = max(1, min(int(limit), 10))
    clauses = []
    params: list = []
    if failure_cause:
        clauses.append("failure_cause = ?")
        params.append(failure_cause)
    if subject:
        clauses.append("payload_json LIKE ?")
        # Wrap in quotes so we match subject appearances as JSON string values
        # (e.g. `"recommended_skills": ["some-skill"]`) without matching
        # arbitrary substrings in narrative text.
        params.append(f'%"{subject}"%')
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with connect_sessions() as conn:
        rows = conn.execute(
            f"""SELECT id, session_id, created_at, attempt, verdict,
                       failure_cause, confidence, scout_viability,
                       execution_mode, payload_json
                FROM post_mortems {where}
                ORDER BY created_at DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def delete_old_post_mortems(cutoff_iso: str) -> int:
    """Delete synthesized post-mortems older than cutoff. Returns rowcount.

    Only touches rows that have already been processed by synthesis — never
    prunes the unsynthesized backlog, regardless of age.
    """
    with connect_sessions() as conn:
        cur = conn.execute(
            "DELETE FROM post_mortems WHERE synthesized_at IS NOT NULL AND created_at < ?",
            (cutoff_iso,),
        )
        return cur.rowcount


def mark_post_mortems_synthesized(pm_ids: list[str]) -> int:
    """Mark post-mortems as processed. Returns rows updated. No-op on empty."""
    if not pm_ids:
        return 0
    placeholders = ",".join(["?"] * len(pm_ids))
    with connect_sessions() as conn:
        cur = conn.execute(
            f"UPDATE post_mortems SET synthesized_at = ? WHERE id IN ({placeholders})",
            [_now(), *pm_ids],
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Tool/skill performance counters (observed from post-mortems)
# ---------------------------------------------------------------------------
#
# One row per (signal_type, subject). Snooze upserts these based on
# post_mortems aggregation. Displayed in Skills and Tools UI sections.
#
# Active signal_types: "tool" and "skill".
# "execution_mode" rows may exist from prior runs — ignored going forward.


def upsert_signal(
    signal_type: str,
    subject: str,
    delta_successes: int = 0,
    delta_failures: int = 0,
    payload_json: str = "{}",
    delta_reinforcements: int = 1,
) -> None:
    """Upsert a signal row. Adds deltas; reinforcements default to +1.

    Call once per observation (e.g. once per post-mortem that touches this
    subject). Pass delta_reinforcements=0 when adding outcome deltas to a
    subject whose usage was already counted elsewhere — adaptive_entry
    usage counts at scout submit-time, outcomes at synthesis time, and
    double-counting the observation would inflate the denominator every
    retirement decision divides by. Does not touch user_approved.
    """
    now = _now()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO scout_signals (
                signal_type, subject, reinforcements, successes, failures,
                first_seen_at, last_reinforced_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_type, subject) DO UPDATE SET
                reinforcements = reinforcements + excluded.reinforcements,
                successes = successes + excluded.successes,
                failures = failures + excluded.failures,
                last_reinforced_at = excluded.last_reinforced_at,
                payload_json = excluded.payload_json""",
            (
                signal_type,
                subject,
                int(delta_reinforcements),
                int(delta_successes),
                int(delta_failures),
                now,
                now,
                payload_json,
            ),
        )


def get_signal(signal_type: str, subject: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT * FROM scout_signals WHERE signal_type = ? AND subject = ?",
            (signal_type, subject),
        ).fetchone()
    return dict(row) if row else None


def get_signals_by_subjects(subjects: list[tuple[str, str]]) -> list[dict]:
    """Lookup performance rows by a list of (signal_type, subject) pairs."""
    if not subjects:
        return []
    placeholders = ",".join(["(?, ?)"] * len(subjects))
    params: list = []
    for t, s in subjects:
        params.extend([t, s])
    with connect_sessions() as conn:
        rows = conn.execute(
            f"SELECT * FROM scout_signals WHERE (signal_type, subject) IN ({placeholders})",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_top_signals(
    since_iso: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return tool and skill performance rows, newest-first.

    Excludes execution_mode rows (no longer tracked).
    limit is a safety cap.
    """
    clauses = ["signal_type IN ('tool', 'skill')"]
    params: list = []
    if since_iso:
        clauses.append("last_reinforced_at >= ?")
        params.append(since_iso)
    where = "WHERE " + " AND ".join(clauses)
    params.append(int(limit))
    with connect_sessions() as conn:
        rows = conn.execute(
            f"""SELECT * FROM scout_signals {where}
               ORDER BY last_reinforced_at DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_model_route_signals(limit: int = 100) -> list[dict]:
    """model_route counter rows (H2, plan §12.4). Subject format is
    "{agent_model}|{task_category}". Ordered by subject for deterministic
    brief rendering (I8 — identical counters → identical bytes)."""
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM scout_signals WHERE signal_type = 'model_route' ORDER BY subject LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_signal(signal_type: str, subject: str) -> bool:
    """Hard-delete a performance row (used for testing and manual cleanup)."""
    with connect_sessions() as conn:
        cur = conn.execute(
            "DELETE FROM scout_signals WHERE signal_type = ? AND subject = ?",
            (signal_type, subject),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# RLM runs (migration v18)
# ---------------------------------------------------------------------------


def create_rlm_run(
    run_id: str,
    session_id: str,
    task: str,
    source_desc: str,
    root_model: str,
    sub_model: str,
    input_chars: int,
    run_dir: str,
    parent_run_id: str | None = None,
    depth: int = 0,
    ui_session_id: str | None = None,
) -> None:
    """Insert an rlm_runs row with status='running'. run_dir is workspace-relative.
    ui_session_id links the run to its sidebar view session (None = no UI surface)."""
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO rlm_runs
               (run_id, session_id, parent_run_id, depth, status, task, source_desc,
                root_model, sub_model, input_chars, run_dir, ui_session_id, created_at)
               VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                session_id,
                parent_run_id,
                depth,
                task[:500],
                source_desc[:500],
                root_model,
                sub_model,
                int(input_chars),
                run_dir,
                ui_session_id,
                _now(),
            ),
        )


def finish_rlm_run(
    run_id: str,
    status: str,
    iterations: int,
    subcalls: int,
    answer_preview: str,
    error: str = "",
) -> None:
    """Record a run's terminal state (completed/iteration_cap/timeout/cancelled/
    budget_exhausted/failed)."""
    with connect_sessions() as conn:
        conn.execute(
            """UPDATE rlm_runs SET
               status = ?, iterations = ?, subcalls = ?,
               answer_preview = ?, error = ?, finished_at = ?
               WHERE run_id = ?""",
            (status, int(iterations), int(subcalls), answer_preview[:500], error[:1000], _now(), run_id),
        )


def get_unsurfaced_rlm_runs(session_id: str, limit: int = 3) -> list[dict]:
    """Terminal depth-0 runs whose outcome never reached the agent.

    A completed run's answer returns through the rlm_process call that made
    it (and is marked surfaced when that tool result is saved), so only
    non-completed terminal runs can be orphans. Nested runs (depth > 0)
    report to their parent engine, never to the session transcript.
    """
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT * FROM rlm_runs
               WHERE session_id = ? AND depth = 0
                 AND finished_at IS NOT NULL AND surfaced_at IS NULL
                 AND status NOT IN ('running', 'completed')
               ORDER BY finished_at DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_rlm_run_surfaced(run_id: str) -> None:
    """Record that the run's outcome reached the agent's transcript."""
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE rlm_runs SET surfaced_at = ? WHERE run_id = ? AND surfaced_at IS NULL",
            (_now(), run_id),
        )


def fail_orphaned_rlm_runs() -> int:
    """Mark rlm_runs rows stuck at status='running' as 'orphaned'.

    Called once at startup: a running
    row across a restart is by definition dead — the engine is synchronous and
    its child self-reaps when the server process goes away. Returns rows updated.
    """
    with connect_sessions() as conn:
        # Park the runs' sidebar view sessions first (same transaction) so
        # their dots stop pulsing on a run that will never finish.
        conn.execute("""UPDATE sessions SET state = 'idle'
               WHERE id IN (SELECT ui_session_id FROM rlm_runs
                            WHERE status = 'running' AND ui_session_id IS NOT NULL)""")
        cur = conn.execute(
            """UPDATE rlm_runs SET status = 'orphaned', finished_at = ?
               WHERE status = 'running' AND finished_at IS NULL""",
            (_now(),),
        )
        return cur.rowcount


def list_rlm_runs(session_id: str | None = None, limit: int = 20, space_id: str | None = None) -> list[dict]:
    """Return RLM runs, newest first. Optionally filter by owning session,
    or by SPACE (v33) — a join through sessions, so every run launched from
    any member session of the space is listed. space_id wins over session_id."""
    with connect_sessions() as conn:
        if space_id:
            rows = conn.execute(
                """SELECT r.* FROM rlm_runs r
                   JOIN sessions s ON s.id = r.session_id
                   WHERE s.space_id = ?
                   ORDER BY r.created_at DESC LIMIT ?""",
                (space_id, limit),
            ).fetchall()
        elif session_id:
            rows = conn.execute(
                """SELECT * FROM rlm_runs WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM rlm_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def list_rlm_runs_before(cutoff_iso: str) -> list[dict]:
    """Runs created before the cutoff that are no longer running — retention
    candidates (the caller deletes the run dir, then the row). Root runs only:
    nested runs live inside their parent's dir and are removed with it."""
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT * FROM rlm_runs
               WHERE created_at < ? AND status != 'running' AND parent_run_id IS NULL
               ORDER BY created_at""",
            (cutoff_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_rlm_run(run_id: str) -> int:
    """Delete a run row and any nested child rows. Returns rows deleted."""
    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM rlm_runs WHERE run_id = ? OR parent_run_id = ?", (run_id, run_id))
        return cur.rowcount


def get_rlm_run(run_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM rlm_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def get_rlm_run_by_ui_session(ui_session_id: str) -> dict | None:
    """The run behind a session_type='rlm' view session (newest if several)."""
    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT * FROM rlm_runs WHERE ui_session_id = ? ORDER BY created_at DESC LIMIT 1",
            (ui_session_id,),
        ).fetchone()
        return dict(row) if row else None


def update_rlm_run_progress(run_id: str, iterations: int, subcalls: int) -> None:
    """Mid-run counter refresh so list/detail readers see live progress.

    Guarded on status='running': subcall progress arrives from broker handler
    threads, and a straggler landing after finish_rlm_run must not overwrite
    the terminal counters."""
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE rlm_runs SET iterations = ?, subcalls = ? WHERE run_id = ? AND status = 'running'",
            (int(iterations), int(subcalls), run_id),
        )


def list_rlm_run_children(parent_run_id: str) -> list[dict]:
    """Nested rlm_query runs of a parent, oldest first."""
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM rlm_runs WHERE parent_run_id = ? ORDER BY created_at",
            (parent_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Skill improvement proposals (migration v14)
# ---------------------------------------------------------------------------


def add_skill_proposal(
    skill_name: str,
    section: str,
    problem: str,
    proposed_change: str,
    confidence: float,
    source_step_id: str = "",
    source_worker_id: str = "",
    source_origin: str = "session",
    session_id: str | None = None,
) -> str:
    """Insert a skill improvement proposal. Returns proposal id.

    `source_origin` is "session" (reflect on a regular session) or "refine".
    The legacy `workflow_name`/`run_id` columns are written NULL: they belong
    to the removed workflow engine and are kept only so historical rows stay
    readable — migrations are forward-only, so the columns outlive the
    feature. Nothing writes them any more.
    """
    pid = _new_id()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO skill_improvement_proposals
               (id, workflow_name, run_id, session_id, source_origin, skill_name,
                section, problem, proposed_change, confidence, source_step_id,
                source_worker_id, status, created_at)
               VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                pid,
                session_id,
                source_origin,
                skill_name,
                section,
                problem,
                proposed_change,
                float(confidence),
                source_step_id,
                source_worker_id,
                _now(),
            ),
        )
    return pid


def list_skill_proposals(
    skill_name: str | None = None,
    status: str | None = None,
    source_origin: str | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses = []
    params: list = []
    if skill_name:
        clauses.append("skill_name = ?")
        params.append(skill_name)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source_origin:
        clauses.append("source_origin = ?")
        params.append(source_origin)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with connect_sessions() as conn:
        rows = conn.execute(
            f"SELECT * FROM skill_improvement_proposals {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_proposal_counts_by_skill() -> dict[str, int]:
    """Return a mapping ``{skill_name: pending_proposal_count}``.

    Single batched ``GROUP BY`` so the Skills API can flag every skill in
    one round trip. Empty dict when there are no pending proposals.
    """
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT skill_name, COUNT(*) AS n FROM skill_improvement_proposals "
            "WHERE status = 'pending' GROUP BY skill_name"
        ).fetchall()
        return {r["skill_name"]: int(r["n"]) for r in rows}


def get_pending_proposals_for_skill(
    skill_name: str,
    min_confidence: float = 0.6,
    limit: int = 3,
) -> list[dict]:
    """Pending proposals for a skill, sorted by confidence desc then recency.

    Used by the stuck-mode peek in sessions/hooks.py — returns proposals the
    agent can try as trial hints. Caller MUST treat these as unapproved and
    call record_proposal_trial_use(...) for each one injected.
    """
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT * FROM skill_improvement_proposals
               WHERE skill_name = ?
                 AND status = 'pending'
                 AND confidence >= ?
               ORDER BY confidence DESC, created_at DESC
               LIMIT ?""",
            (skill_name, float(min_confidence), limit),
        ).fetchall()
        return [dict(r) for r in rows]


def record_proposal_trial_use(proposal_id: str) -> None:
    """Increment trial_uses counter and bump last_trial_at."""
    with connect_sessions() as conn:
        conn.execute(
            """UPDATE skill_improvement_proposals
               SET trial_uses = trial_uses + 1, last_trial_at = ?
               WHERE id = ?""",
            (_now(), proposal_id),
        )


def record_proposal_trial_success(proposal_id: str) -> None:
    """Increment trial_successes counter."""
    with connect_sessions() as conn:
        conn.execute(
            """UPDATE skill_improvement_proposals
               SET trial_successes = trial_successes + 1
               WHERE id = ?""",
            (proposal_id,),
        )


def get_skill_proposal(proposal_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM skill_improvement_proposals WHERE id = ?", (proposal_id,)).fetchone()
        return dict(row) if row else None


def resolve_skill_proposal(proposal_id: str, status: str) -> bool:
    """Mark a proposal as approved, rejected, applied, auto_applied, or
    archived. Returns True if row existed.

    'applied' = a human clicked Apply; 'auto_applied' = the veto-window
    sweep applied it (core/skills/proposals.py:auto_apply_ripe_proposals).
    Distinct statuses so the daily auto-apply cap can count its own work.
    """
    if status not in ("approved", "rejected", "applied", "auto_applied", "archived"):
        raise ValueError(f"Invalid proposal status: {status!r}")
    with connect_sessions() as conn:
        cur = conn.execute(
            "UPDATE skill_improvement_proposals SET status = ?, resolved_at = ? WHERE id = ?",
            (status, _now(), proposal_id),
        )
        return cur.rowcount > 0


def count_auto_applied_skill_proposals_since(cutoff_iso: str) -> int:
    """How many proposals the veto-window sweep applied since `cutoff_iso`.

    Backs the ``skill_proposal_max_auto_applies_per_day`` budget — same
    counting pattern as adaptive_count_auto_approved_since.
    """
    with connect_sessions() as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM skill_improvement_proposals
               WHERE status = 'auto_applied' AND resolved_at >= ?""",
            (cutoff_iso,),
        ).fetchone()
        return int(row[0]) if row else 0


def archive_proposals_for_run(run_id: str) -> int:
    """Archive all pending proposals associated with a deleted run."""
    with connect_sessions() as conn:
        cur = conn.execute(
            """UPDATE skill_improvement_proposals
               SET status = 'archived', resolved_at = ?
               WHERE run_id = ? AND status = 'pending'""",
            (_now(), run_id),
        )
        return cur.rowcount


def get_db_stats() -> dict:
    with connect_sessions() as conn:
        tables = {}
        for table in [
            "sessions",
            "messages",
            "artifacts",
            "token_usage",
            "questions",
            "notifications",
            "session_messages",
            "cron_runs",
        ]:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            tables[table] = row["cnt"]
        # DB file size
        row = conn.execute("PRAGMA page_count").fetchone()
        page_count = row[0] if row else 0
        row = conn.execute("PRAGMA page_size").fetchone()
        page_size = row[0] if row else 4096
        tables["db_size_bytes"] = page_count * page_size
    return tables


# ---------------------------------------------------------------------------
# Dream (idle-time introspection add-on — core/dream)
# ---------------------------------------------------------------------------

DREAM_HYPOTHESIS_KINDS = frozenset(
    {"contradiction", "lesson_ineffective", "tool_pattern", "memory_stale", "open_question"}
)
DREAM_HYPOTHESIS_STATUSES = frozenset({"pending", "validated", "refuted", "expired", "promoted", "archived"})


def add_dream_hypothesis(
    kind: str,
    statement: str,
    evidence_json: str,
    origin: str = "dream_cycle",
    confidence: float = 0.0,
) -> str:
    """Insert a dream hypothesis (status=pending). Returns id."""
    hid = _new_id()
    now = _now()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO dream_hypotheses
               (id, kind, statement, evidence_json, status, confidence, origin,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (hid, kind, statement, evidence_json, float(confidence), origin, now, now),
        )
    return hid


def list_dream_hypotheses(
    status: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    oldest_first: bool = False,
    exclude_kinds: Iterable[str] | None = None,
) -> list[dict]:
    """List hypotheses, newest-first by default. `oldest_first` orders (and
    therefore windows) from the other end — queue consumers must use it, or
    a backlog larger than `limit` permanently starves its oldest rows.

    `exclude_kinds` filters **inside** the query, which matters for exactly
    the same reason `oldest_first` does. A caller that windows to `limit` and
    then drops rows in Python is windowing over the wrong population: kinds
    it never wanted can fill the entire window and leave it with nothing,
    which is indistinguishable from an empty queue. That is not theoretical —
    `open_question` rows are never validated and never expire, and once 200
    of them accumulated they silently starved the validator (see
    core/dream/__init__.py)."""
    clauses = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    excluded = [k for k in (exclude_kinds or []) if k]
    if excluded:
        clauses.append(f"kind NOT IN ({','.join('?' for _ in excluded)})")
        params.extend(excluded)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = "ASC" if oldest_first else "DESC"
    params.append(limit)
    with connect_sessions() as conn:
        rows = conn.execute(
            f"SELECT * FROM dream_hypotheses {where} ORDER BY created_at {order} LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def update_dream_hypothesis(
    hypothesis_id: str,
    *,
    status: str | None = None,
    confidence: float | None = None,
    validation_json: str | None = None,
    promoted_ref: str | None = None,
) -> bool:
    """Update mutable fields on a hypothesis. Returns True if a row changed."""
    sets = ["updated_at = ?"]
    params: list = [_now()]
    if status is not None:
        if status not in DREAM_HYPOTHESIS_STATUSES:
            raise ValueError(f"invalid dream hypothesis status: {status}")
        sets.append("status = ?")
        params.append(status)
    if confidence is not None:
        sets.append("confidence = ?")
        params.append(float(confidence))
    if validation_json is not None:
        sets.append("validation_json = ?")
        params.append(validation_json)
    if promoted_ref is not None:
        sets.append("promoted_ref = ?")
        params.append(promoted_ref)
    params.append(hypothesis_id)
    with connect_sessions() as conn:
        cur = conn.execute(
            f"UPDATE dream_hypotheses SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        return cur.rowcount > 0


def archive_stale_dream_hypotheses(kind: str, max_age_days: int) -> int:
    """Retire `pending` rows of a kind that has no validation path.

    `open_question` is report material: it is deliberately excluded from
    validation, so nothing ever moves it out of `pending` and the rows
    accumulate forever. Left alone they are not merely clutter — they crowd
    the validator's oldest-first window and starve the kinds that *do* have
    a path. Archiving keeps them readable (the report groups `archived`) and
    terminal, so the queue converges.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))).isoformat()
    with connect_sessions() as conn:
        cur = conn.execute(
            """UPDATE dream_hypotheses SET status = 'archived', updated_at = ?
               WHERE kind = ? AND status = 'pending' AND created_at < ?""",
            (_now(), kind, cutoff),
        )
        return cur.rowcount


def count_dream_hypotheses(status: str | None = None, kind: str | None = None) -> int:
    """Row count without paying to materialize the rows. Used by the queue
    health check, which needs the true population size rather than a
    windowed sample."""
    clauses = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect_sessions() as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM dream_hypotheses {where}", params).fetchone()
        return int(row[0]) if row else 0


def add_dream_report(period_start: str, period_end: str, path: str, stats_json: str) -> str:
    """Insert a dream report row. Returns id."""
    rid = _new_id()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO dream_reports (id, created_at, period_start, period_end, path, stats_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rid, _now(), period_start, period_end, path, stats_json),
        )
    return rid


def list_dream_reports(limit: int = 20) -> list[dict]:
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM dream_reports ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_post_mortems_since(created_after: str, limit: int = 20) -> list[dict]:
    """Post-mortems newer than the cursor, oldest first (dream evidence feed)."""
    with connect_sessions() as conn:
        rows = conn.execute(
            "SELECT * FROM post_mortems WHERE created_at > ? ORDER BY created_at ASC LIMIT ?",
            (created_after, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Background jobs (job_start / job_status / job_tail / job_kill)
# ---------------------------------------------------------------------------


def create_job(
    job_id: str,
    session_id: str,
    name: str,
    command: str,
    pid: int,
    log_path: str,
    deadline_s: int,
) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO jobs (id, session_id, name, command, pid, state,
                                 created_at, deadline_s, log_path)
               VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
            (job_id, session_id, name, command, pid, now, deadline_s, log_path),
        )


def update_job(job_id: str, **kwargs) -> None:
    allowed = {"state", "exit_code", "finished_at", "pid"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k} = ?" for k in updates)
    with connect_sessions() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*updates.values(), job_id))


def get_job(job_id: str) -> dict | None:
    with connect_sessions() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_jobs(session_id: str | None = None, limit: int = 20) -> list[dict]:
    with connect_sessions() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
