"""Pernix — memory-file compaction: drop tombstones that carry nothing.

Memory files are append-only plus tags. Archiving an entry (dedup, prune,
move) tags its section `@archived` and leaves it in the file; nothing ever
removed one. Moves made it worse: every sweep move left a tombstone in the
source, and the split/reroute and split/consolidate loops moved the same
bodies thousands of times. By 2026-11-06 the box store was 143 MB with 9 MB
live; user.profile.md held 61 live entries in 45.5 MB.

A tombstone is worth keeping only as the last record of a body that is not
live anywhere — the undo copy of a fused or pruned entry. Compaction keeps
exactly one such copy per body and drops the rest:

  * every LIVE section is kept byte-for-byte;
  * an archived section (or any section of an archived file) is dropped when
    its body is live in any file, or when another archived copy of the body
    was already kept (per store when `seen_dead` is shared across files);
  * stacked `@archived` tags collapse to one.

Every rewrite is checked: the file's live (epoch, content) list must be
identical before and after, or the file is left untouched.
"""

from __future__ import annotations

import logging
import re

from core.memory.format import _section_body, is_file_archived, parse_entries_from_markdown
from core.memory.moves import body_key

logger = logging.getLogger("pernix.memory.compact")

_ARCHIVED = "<!-- @archived: true -->"
_STACKED = re.compile(r"(?:<!-- @archived: true -->\n?){2,}")


def _live_signature(file_name: str, raw: str) -> list[tuple[int, str]]:
    return [(e.epoch, e.content) for e in parse_entries_from_markdown(file_name, raw)]


def compact_text(file_name: str, raw: str, live_keys: set[str], seen_dead: set[str]) -> tuple[str, dict]:
    """Compacted markdown for one file plus stats. Returns `raw` unchanged
    when nothing is dropped or the live-set check fails."""
    stats = {"dropped_live_twin": 0, "dropped_extra_copy": 0, "kept_tombstones": 0, "collapsed_tags": 0}
    sections = raw.split("\n---\n")
    if len(sections) < 2:
        return raw, stats
    file_archived = is_file_archived(raw)
    out = [sections[0]]
    changed = False
    for section in sections[1:]:
        archived = file_archived or _ARCHIVED in section
        if not archived or "<!-- @epoch:" not in section:
            out.append(section)
            continue
        body = _section_body(section)
        key = body_key(body)
        if key in live_keys:
            stats["dropped_live_twin"] += 1
            changed = True
            continue
        if key in seen_dead:
            stats["dropped_extra_copy"] += 1
            changed = True
            continue
        seen_dead.add(key)
        if _STACKED.search(section):
            section = _STACKED.sub(_ARCHIVED + "\n", section)
            stats["collapsed_tags"] += 1
            changed = True
        stats["kept_tombstones"] += 1
        out.append(section)
    if not changed:
        return raw, stats
    new_raw = "\n---\n".join(out)
    if not new_raw.endswith("\n") and raw.endswith("\n"):
        new_raw += "\n"
    if _live_signature(file_name, new_raw) != _live_signature(file_name, raw):
        logger.warning("Compaction of %s skipped: live set would change", file_name)
        return raw, {k: 0 for k in stats}
    return new_raw, stats


def live_body_keys(store) -> set[str]:
    """Keys of every body live in the index (the source of 'live anywhere')."""
    conn = store._connect()
    try:
        return {body_key(row[0]) for row in conn.execute("SELECT content FROM memory_fts")}
    finally:
        conn.close()


def compact_files(
    store,
    names: list[str],
    *,
    live_keys: set[str] | None = None,
    seen_dead: set[str] | None = None,
    is_cancelled=lambda: False,
) -> dict:
    """Compact the named files through store.rewrite_file (the store lock).

    Pass one `seen_dead` across a whole-store run to keep a single tombstone
    per dead body store-wide; the recurring rung passes a fresh set per file
    (one per file) because it only sees a few files per cycle.
    """
    live_keys = live_body_keys(store) if live_keys is None else live_keys
    totals = {
        "files": 0,
        "bytes_before": 0,
        "bytes_after": 0,
        "dropped_live_twin": 0,
        "dropped_extra_copy": 0,
        "kept_tombstones": 0,
        "collapsed_tags": 0,
    }
    for name in names:
        if is_cancelled():
            break
        per_file = set() if seen_dead is None else seen_dead
        result: dict = {}

        def transform(raw: str, _name=name, _seen=per_file, _out=result) -> str | None:
            new_raw, stats = compact_text(_name, raw, live_keys, _seen)
            _out.update(stats, before=len(raw.encode()), after=len(new_raw.encode()))
            return new_raw if new_raw != raw else None

        try:
            store.rewrite_file(name, transform)
        except Exception as e:
            logger.warning("Compaction of %s failed: %s", name, e)
            continue
        if result:
            totals["files"] += 1
            totals["bytes_before"] += result.pop("before")
            totals["bytes_after"] += result.pop("after")
            for k, v in result.items():
                totals[k] += v
    return totals


def files_by_tombstones(store, limit: int) -> list[str]:
    """Memory files ordered by on-disk size beyond their live share — the
    ones compaction helps most. Cheap: size and index counts only."""
    rows = []
    for md_path in store._dir.glob("*.md"):
        try:
            size = md_path.stat().st_size
        except OSError:
            continue
        rows.append((size, md_path.stem))
    rows.sort(reverse=True)
    return [name for _, name in rows[:limit]]
