"""Pernix — the move ledger: one record per entry body a sweep moved.

Three snooze sweeps move entries between memory files (split_file,
reroute_misplaced_entries, consolidate_files) and each judges placement its
own way. Two pairs of them undid each other for months on the box: split
peeled profile entries out of user.profile and reroute pulled them back;
consolidate merged sibling files and split broke the result apart again. One
body was moved 144 times in a month, and every move left a tombstone — the
store reached 143 MB with 9 MB live (2026-11-06 review).

The ledger is the backstop that holds for any pair of rules, including ones
nobody has written yet. A sweep move is refused when the body:
  * moved within `memory_move_cooldown_days`;
  * would go back to the file its last move came from (any age);
  * moved `memory_move_breaker` times in the last 30 days.
A move into the entry type's home (routing.home_for) is exempt from the
reversal and breaker rules — the home is a fixed point no sweep moves out of —
but still waits out the cooldown.

Keyed on content only: tags change under enrich_tags, epochs change on
collision, and a corrected entry (update_entry) is new evidence that may move.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

_WS = re.compile(r"\s+")
_DAY = 86400


def body_key(content: str) -> str:
    """Stable key for an entry body: sha1 of whitespace-collapsed content."""
    return hashlib.sha1(_WS.sub(" ", (content or "").strip()).encode("utf-8")).hexdigest()


@dataclass
class MoveHistory:
    last_src: str
    last_dst: str
    last_at: int
    recent: int  # moves in the last 30 days


def load_history(conn, keys: list[str] | None = None) -> dict[str, MoveHistory]:
    """Move history per body key (all keys when `keys` is None)."""
    now = int(time.time())
    out: dict[str, MoveHistory] = {}
    if keys is not None and not keys:
        return out
    if keys is None:
        rows = conn.execute("SELECT body_key, src, dst, at FROM memory_moves ORDER BY at ASC, id ASC").fetchall()
    else:
        rows = []
        uniq = list(dict.fromkeys(keys))
        for i in range(0, len(uniq), 500):
            chunk = uniq[i : i + 500]
            rows += conn.execute(
                "SELECT body_key, src, dst, at FROM memory_moves WHERE body_key IN (%s) ORDER BY at ASC, id ASC"
                % ",".join("?" * len(chunk)),
                chunk,
            ).fetchall()
    for key, src, dst, at in rows:
        h = out.get(key)
        recent = 1 if at >= now - 30 * _DAY else 0
        if h is None:
            out[key] = MoveHistory(src, dst, at, recent)
        else:
            h.last_src, h.last_dst, h.last_at = src, dst, at
            h.recent += recent
    return out


def refusal(history: MoveHistory | None, src: str, dst: str, *, entry_type: str = "", now: int | None = None) -> str:
    """Why the ledger refuses moving a body src → dst, or "" when it allows it."""
    if history is None:
        return ""
    from config import settings
    from core.memory.routing import home_for, home_ok

    now = now or int(time.time())
    cooldown = max(0, int(settings.memory_move_cooldown_days)) * _DAY
    if cooldown and now - history.last_at < cooldown:
        return "cooldown"
    # Into the type's home: a fixed point no sweep moves out of, so neither a
    # reversal nor a repeat can start a loop from here.
    if home_for(entry_type, src) and not home_ok(entry_type, src) and home_ok(entry_type, dst):
        return ""
    if history.last_src == dst and history.last_dst == src:
        return "reversal"
    if history.recent >= max(1, int(settings.memory_move_breaker)):
        return "breaker"
    return ""


def record(conn, rows: list[tuple[str, str, str, str]]) -> None:
    """Append (body_key, src, dst, reason) rows. Caller commits."""
    now = int(time.time())
    conn.executemany(
        "INSERT INTO memory_moves (body_key, src, dst, at, reason) VALUES (?, ?, ?, ?, ?)",
        [(k, s, d, now, r) for k, s, d, r in rows],
    )


def seed_from_markdown(conn, entries_with_meta: list[tuple[str, str, str, int]]) -> int:
    """Seed the ledger from @merged_from/@merged_at of live entries.

    entries_with_meta: (content, merged_from, current_file, merged_at). Only
    the most recent hop is known from the markdown; that is enough for the
    cooldown and reversal rules to hold from the first cycle after deploy.
    """
    rows = [
        (body_key(c), src, dst, int(at), "seed")
        for c, src, dst, at in entries_with_meta
        if src and dst and src != dst and at
    ]
    conn.executemany("INSERT INTO memory_moves (body_key, src, dst, at, reason) VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


_MERGED_FROM = re.compile(r"<!-- @merged_from:\s*([^\s]+)\s*-->")
_MERGED_AT = re.compile(r"<!-- @merged_at:\s*(\d+)\s*-->")


def seed_from_store(store) -> int:
    """One-time: seed the ledger from the last hop recorded in live entries.

    Without it the ledger starts empty on deploy, and every body the old
    loops had just moved would be free to move once more.
    """
    from core.memory.format import _section_body, is_file_archived

    rows: list[tuple[str, str, str, int]] = []
    for md_path in sorted(store._dir.glob("*.md")):
        try:
            raw = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if is_file_archived(raw):
            continue
        for section in raw.split("\n---\n")[1:]:
            if "<!-- @archived: true -->" in section:
                continue
            src = _MERGED_FROM.search(section)
            at = _MERGED_AT.search(section)
            if src and at:
                rows.append((_section_body(section), src.group(1), md_path.stem, int(at.group(1))))
    conn = store._connect()
    try:
        n = seed_from_markdown(conn, rows)
        conn.commit()
    finally:
        conn.close()
    return n


def stats(conn, *, hours: int = 24) -> dict:
    """Moves in the last `hours`, and bodies at or past the breaker."""
    from config import settings

    now = int(time.time())
    moved = conn.execute(
        "SELECT COUNT(*) FROM memory_moves WHERE at >= ? AND reason != 'seed'", (now - hours * 3600,)
    ).fetchone()[0]
    at_breaker = conn.execute(
        "SELECT COUNT(*) FROM (SELECT body_key FROM memory_moves WHERE at >= ? GROUP BY body_key HAVING COUNT(*) >= ?)",
        (now - 30 * _DAY, max(1, int(settings.memory_move_breaker))),
    ).fetchone()[0]
    return {"moves_24h": moved, "bodies_at_breaker": at_breaker}
