"""Pernix — Memory-store maintenance sweeps.

The memory-store surgery that used to live inside ``core/snooze.py``: dedup,
cross-file consolidation, entry re-routing, tag enrichment, FTS5 index
reconciliation, file splitting, and staleness pruning. Snooze owns *when*
these run (the idle gate and the activity ladder); this module owns *what*
they do.

Every entry point is a plain async function taking the collaborators it needs
explicitly — the store, the ``db`` module, a ``is_cancelled()`` poll, and its
budgets — so each sweep is callable and testable without a SnoozeRunner.
Returns are stat deltas the caller folds into its own counters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Callable

from config import settings

logger = logging.getLogger("pernix.memory.sweeps")


# ---------------------------------------------------------------------------
# Archive / index helpers
# ---------------------------------------------------------------------------


def archive_entries(store, file_name: str, epochs: set[int]) -> None:
    """Archive entries in markdown AND drop them from the FTS index.

    These two halves were always invoked back-to-back but as separate
    synchronous calls from async code. That ran them on the event loop
    (blocking every session's SSE) and left a window between them: a cancel or
    crash landing in the middle archived the markdown while the index still
    served the entry, so recall returned rows whose bodies were tagged
    archived. Snooze is the only writer that splits an entry across both
    stores, so it is the only place that drift originates.

    Callers dispatch this via asyncio.to_thread. to_thread cannot be
    cancelled, so once started both halves run to completion — the pairing is
    atomic with respect to cancellation, which is what closes the window
    rather than merely narrowing it.
    """
    _archive_entries_in_file(store, file_name, epochs)
    _remove_from_index(store, file_name, epochs)


def _archive_entries_in_file(store, file_name: str, epochs: set[int]) -> None:
    """Add <!-- @archived: true --> tag to entries in markdown file.

    Prefer archive_entries() — calling this without the index removal leaves
    markdown and FTS disagreeing.

    Goes through store.rewrite_file rather than reaching into store._dir /
    store._lock: the sweeps' private-state copy of read-modify-write is what
    duplicated the flock-after-truncate race into this module.
    """

    def _tag_archived(content: str) -> str:
        for epoch in epochs:
            # Find the epoch comment and add archived tag after it
            pattern = f"<!-- @epoch: {epoch} -->"
            if pattern in content:
                content = content.replace(
                    pattern,
                    f"{pattern}\n<!-- @archived: true -->",
                )
        return content

    store.rewrite_file(file_name, _tag_archived)


def _remove_from_index(store, file_name: str, epochs: set[int]) -> None:
    """Remove archived entries from FTS5 index and clean up associated hit records."""
    conn = store._connect()
    try:
        for epoch in epochs:
            conn.execute(
                "DELETE FROM memory_fts WHERE file_name = ? AND epoch = ?",
                (file_name, str(epoch)),
            )
            # Also remove any hit-count records for this entry so memory_hits
            # doesn't accumulate orphan rows for epochs no longer in FTS5.
            conn.execute(
                "DELETE FROM memory_hits WHERE file_name = ? AND epoch = ?",
                (file_name, str(epoch)),
            )
        # Recount from FTS5 as the authoritative source
        remaining = conn.execute(
            "SELECT COUNT(*) as cnt FROM memory_fts WHERE file_name = ?",
            (file_name,),
        ).fetchone()
        if remaining:
            conn.execute(
                "UPDATE memory_files SET entry_count = ?, updated_at = ? WHERE name = ?",
                (remaining["cnt"], int(time.time()), file_name),
            )
        conn.commit()
    finally:
        conn.close()


def _strip_fence(text: str) -> str:
    """Drop a leading ``` fence line (and a trailing one) from an LLM reply."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return text


# ---------------------------------------------------------------------------
# Activity 3: deduplication sweep (no LLM)
# ---------------------------------------------------------------------------


async def dedup_sweep(store, db, is_cancelled: Callable[[], bool], *, interval_days: int) -> int:
    """Scan one memory file for near-duplicate entries. Returns entries archived."""
    from core.memory.format import parse_entries_from_markdown

    if not store:
        return 0

    # Find a file due for dedup
    interval_seconds = interval_days * 86400
    files = await asyncio.to_thread(store.list_files)

    target_file = None
    for f in files:
        if f.entry_count < 5:
            continue
        key = f"dedup_{f.name}"
        last_sweep = db.get_snooze_state(key)
        if last_sweep:
            try:
                last_dt = datetime.fromisoformat(last_sweep)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < interval_seconds:
                    continue
            except ValueError:
                pass
        target_file = f
        break

    if not target_file:
        return 0

    if is_cancelled():
        return 0

    logger.info("Snooze: dedup sweep on %s (%d entries)", target_file.name, target_file.entry_count)

    # Parse entries from markdown
    md_content = await asyncio.to_thread(store.read_file, target_file.name)
    if not md_content:
        return 0

    entries = parse_entries_from_markdown(target_file.name, md_content)
    if len(entries) < 2:
        db.set_snooze_state(f"dedup_{target_file.name}", datetime.now(timezone.utc).isoformat())
        return 0

    archived_epochs = await asyncio.to_thread(_pairwise_dedup, entries, is_cancelled)

    deduped = 0
    if archived_epochs:
        # Markdown archive-tag + FTS removal as one uncancellable unit.
        await asyncio.to_thread(archive_entries, store, target_file.name, archived_epochs)
        deduped = len(archived_epochs)
        logger.info("Snooze: archived %d duplicates in %s", deduped, target_file.name)

    db.set_snooze_state(f"dedup_{target_file.name}", datetime.now(timezone.utc).isoformat())
    return deduped


def _pairwise_dedup(entries: list, is_cancelled: Callable[[], bool]) -> set[int]:
    """Pairwise similarity check; returns the epochs to archive.

    Runs off-loop (asyncio.to_thread) so the event loop stays responsive even
    on large files, and uses SequenceMatcher's O(1) real_quick_ratio() and
    O(N+M) quick_ratio() as upper-bound prescreens — pairs that can't reach
    0.82 are skipped without ever computing the full O(N·M) ratio.
    """
    from core.memory.dedup import loses_no_unique_token

    archived: set[int] = set()
    for i in range(len(entries)):
        if is_cancelled():
            break
        if entries[i].epoch in archived:
            continue
        for j in range(i + 1, len(entries)):
            if entries[j].epoch in archived:
                continue
            sm = SequenceMatcher(None, entries[i].content, entries[j].content)
            if sm.real_quick_ratio() < 0.82 or sm.quick_ratio() < 0.82:
                continue
            sim = sm.ratio()
            if sim > 0.82:
                to_archive = entries[j] if len(entries[j].content) <= len(entries[i].content) else entries[i]
                to_keep = entries[i] if to_archive is entries[j] else entries[j]
                # Ratio alone is not enough — see loses_no_unique_token, the
                # shared guard every entry-destroying operation must clear.
                if not loses_no_unique_token(to_archive.content, to_keep.content):
                    continue
                archived.add(to_archive.epoch)
                logger.debug("Snooze: archiving duplicate (epoch=%d, sim=%.2f)", to_archive.epoch, sim)
    return archived


# ---------------------------------------------------------------------------
# Activity 3b: cross-file consolidation
# ---------------------------------------------------------------------------


async def consolidate_files(
    store,
    db,
    is_cancelled: Callable[[], bool],
    *,
    did_llm_already: bool,
    llm_ready: Callable[[], bool],
    interval_hours: int,
) -> tuple[bool, int]:
    """Consolidate overlapping memory files.

    Returns (used_llm, files_consolidated).
    """
    from core.memory.consolidate import (
        build_llm_merge_prompt,
        build_signatures,
        execute_merge,
        find_clusters,
        parse_llm_merge_response,
        plan_trivial_merge,
        prioritize_clusters,
    )

    if not store:
        return False, 0

    # Rate limit: check interval
    interval_seconds = interval_hours * 3600
    last = db.get_snooze_state("last_consolidation_scan")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < interval_seconds:
                return False, 0
        except ValueError:
            pass

    if is_cancelled():
        return False, 0

    # Phase 1: Build signatures and find clusters (no LLM).
    # The pairwise SequenceMatcher in find_clusters is CPU-heavy on realistic
    # stores — push it onto a worker thread so the asyncio loop stays
    # responsive (HTTP, SSE heartbeats, snooze timeout).
    signatures = await asyncio.to_thread(build_signatures, store)
    if len(signatures) < 2:
        db.set_snooze_state("last_consolidation_scan", datetime.now(timezone.utc).isoformat())
        return False, 0

    sig_map = {s.name: s for s in signatures}
    clusters = await asyncio.to_thread(find_clusters, signatures, None, is_cancelled)

    if not clusters:
        db.set_snooze_state("last_consolidation_scan", datetime.now(timezone.utc).isoformat())
        return False, 0

    clusters = prioritize_clusters(clusters, sig_map)

    if is_cancelled():
        return False, 0

    # Phase 2: Process ONE cluster per cycle
    cluster = clusters[0]
    logger.info("Snooze: consolidating cluster %s (%d files)", cluster, len(cluster))

    used_llm = False
    consolidated = 0

    # Try trivial merge first (no LLM). Also CPU-heavy when a cluster has many
    # entries — same offload reasoning as Phase 1.
    decision = await asyncio.to_thread(plan_trivial_merge, cluster, store)

    if decision is None and not did_llm_already and llm_ready():
        # Need LLM for ambiguous merge
        prompt = build_llm_merge_prompt(cluster, store)
        try:
            from core.llm.client import get_llm_client

            client = get_llm_client()
            response = await client.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent."},
                    {"role": "user", "content": prompt},
                ],
                model=settings.background_model or settings.llm_model,
                max_tokens=2000,
            )
            decision = parse_llm_merge_response(response.content.strip(), cluster)
            used_llm = True
        except Exception as e:
            logger.warning("Snooze: consolidation LLM call failed: %s", e)

    if decision:
        await asyncio.to_thread(execute_merge, store, decision)
        consolidated = len(decision.source_files)
        logger.info(
            "Snooze: consolidated %d files into %s (%s)",
            consolidated,
            decision.target_file,
            decision.strategy,
        )

    db.set_snooze_state("last_consolidation_scan", datetime.now(timezone.utc).isoformat())
    return used_llm, consolidated


# ---------------------------------------------------------------------------
# Activity 3c: entry re-routing
# ---------------------------------------------------------------------------


REROUTE_PROMPT = """You are a memory file auditor. Review these memory entries that may be stored in the wrong file.

EXISTING MEMORY FILES:
{file_catalog}

ENTRIES TO REVIEW:
{entry_list}

For each entry, decide: keep in the current file, move to an existing file, or group with others into a new file.

ROUTING GUIDANCE:
- Personal info about the user (name, location, employer, hardware, preferences) → user identity/profile file
- System design, components, agent loop, workers, tool schemas, deployment → Pernix config/architecture file
- Operational lessons, mistakes, recovery patterns, critical gotchas → lessons or debugging file
- Tool usage patterns, code workflows, command recipes → tools or patterns file
- External findings, third-party analysis → research file
- Skill-specific content → matching skill file only; general patterns go to lessons/tools

FILE CREATION RULES:
- PREFER existing files whenever a reasonable match exists.
- You MAY suggest a new file name ONLY if 2 or more entries in this batch share a coherent
  topic that no existing file covers. A single orphan entry does not justify a new file —
  keep it or move it to the closest existing file instead.
- New file names must be dot-separated lowercase: e.g. "pernix.vision", "user.hardware", "pernix.auth".
- Keep new names short (2-3 segments). Do not create near-duplicates of existing files.

Output a JSON array — one entry per reviewed item:
[{{"epoch": <number>, "action": "keep|move", "target_file": "filename", "reason": "brief reason"}}]

For "keep", set target_file to the current file name.
Output valid JSON only. No markdown fences. /no_think"""


def build_file_keywords(files) -> dict[str, set[str]]:
    """Per-file keyword sets for affinity scoring.

    Combines file metadata keywords + file-name segments + NAMESPACE_KEYWORDS.
    """
    from core.memory.store import NAMESPACE_KEYWORDS

    file_keywords: dict[str, set[str]] = {}
    for f in files:
        kws: set[str] = set()
        kws.update(kw.lower().strip() for kw in f.keywords if len(kw.strip()) > 2)
        kws.update(part for part in re.split(r"[._-]", f.name.lower()) if len(part) > 2)
        for ns, ns_kws in NAMESPACE_KEYWORDS.items():
            # Match namespace to file if name is equal or shares the first segment
            if f.name == ns or ns.split(".")[0] == f.name.split(".")[0]:
                kws.update(ns_kws)
        file_keywords[f.name] = kws
    return file_keywords


def classify_entry(entry, src_file: str, file_keywords: dict[str, set[str]]) -> dict | None:
    """Judge one entry's placement. Returns a candidate dict, or None to keep.

    Two checks, in order: type-file consistency (high confidence), then
    tag/keyword affinity scoring (medium — the current file scores zero while
    some other file scores at least 1.0).
    """
    if entry.entry_type == "profile" and src_file != "user.profile":
        return {
            "entry": entry,
            "src_file": src_file,
            "target_file": "user.profile",
            "confidence": "high",
            "reason": "profile type outside user.profile",
        }

    tag_str = " ".join(entry.tags).lower()
    content_lower = entry.content.lower()

    scores: dict[str, float] = {}
    for fname, fkws in file_keywords.items():
        tag_hits = sum(2.0 for kw in fkws if kw in tag_str)
        content_hits = sum(0.5 for kw in fkws if kw in content_lower)
        scores[fname] = tag_hits + content_hits

    current_score = scores.get(src_file, 0.0)
    other = {k: v for k, v in scores.items() if k != src_file}
    if not other:
        return None
    best_other = max(other, key=other.get)
    best_score = other[best_other]

    if best_score < 1.0 or current_score != 0.0:
        return None
    return {
        "entry": entry,
        "src_file": src_file,
        "target_file": best_other,
        "confidence": "medium",
        "reason": (f"no keyword affinity with {src_file}; score {best_score:.1f} for {best_other}"),
    }


def scan_for_reroute_candidates(
    store,
    files,
    file_keywords: dict[str, set[str]],
    max_epoch: int,
    is_cancelled: Callable[[], bool],
) -> tuple[list[dict], list[dict]]:
    """Score every settled entry against every file. Returns (high, medium).

    O(entries × files × keywords) and reads every markdown file from disk —
    callers push this onto a worker thread so the event loop stays responsive,
    and pass a cancel poll so snooze can bail early when work arrives.
    """
    from core.memory.format import parse_entries_from_markdown

    high: list[dict] = []
    medium: list[dict] = []
    for mem_file in files:
        if is_cancelled():
            break
        if mem_file.entry_count < 2:
            continue

        md_content = store.read_file(mem_file.name)
        if not md_content:
            continue

        for entry in parse_entries_from_markdown(mem_file.name, md_content):
            if is_cancelled():
                break
            if entry.epoch > max_epoch:
                continue
            candidate = classify_entry(entry, mem_file.name, file_keywords)
            if candidate is None:
                continue
            (high if candidate["confidence"] == "high" else medium).append(candidate)
    return high, medium


async def reroute_misplaced_entries(
    store,
    db,
    is_cancelled: Callable[[], bool],
    *,
    did_llm_already: bool,
    llm_ready: Callable[[], bool],
    interval_hours: int,
) -> tuple[bool, int]:
    """Move entries that belong in a different file.

    Two passes:
    1. No-LLM: type-consistency check + tag/keyword affinity scoring.
       Clear mismatches (current file score=0, best other score>=1) move immediately.
    2. LLM (if available and not used this cycle): medium-confidence candidates
       are reviewed against the full file catalog and routing rules.

    Returns (used_llm, entries_rerouted).
    """
    from core.memory.ingest import _build_file_catalog

    if not store:
        return False, 0

    # Rate limit: share the consolidation interval so the two never run together
    interval_seconds = interval_hours * 3600
    last = db.get_snooze_state("last_reroute_scan")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < interval_seconds:
                return False, 0
        except ValueError:
            pass

    if is_cancelled():
        return False, 0

    files = await asyncio.to_thread(store.list_files)
    if len(files) < 2:
        db.set_snooze_state("last_reroute_scan", datetime.now(timezone.utc).isoformat())
        return False, 0

    file_keywords = build_file_keywords(files)
    one_day_ago = int(time.time()) - 86400

    high_conf, medium_conf = await asyncio.to_thread(
        scan_for_reroute_candidates, store, files, file_keywords, one_day_ago, is_cancelled
    )

    if not high_conf and not medium_conf:
        db.set_snooze_state("last_reroute_scan", datetime.now(timezone.utc).isoformat())
        return False, 0

    used_llm = False
    rerouted = 0

    # ── Pass 1: high-confidence reroutes (no LLM) ───────────────────
    for item in high_conf:
        if is_cancelled():
            break
        entry = item["entry"]
        src, dst = item["src_file"], item["target_file"]
        try:
            if store.move_entries(src, dst, [entry.epoch]):
                await asyncio.to_thread(archive_entries, store, src, {entry.epoch})
                rerouted += 1
                logger.info(
                    "Snooze: rerouted entry (type=%s epoch=%d) %s → %s",
                    entry.entry_type,
                    entry.epoch,
                    src,
                    dst,
                )
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("Snooze: reroute failed (epoch=%d): %s", entry.epoch, e)

    # ── Pass 2: LLM review for medium-confidence candidates ──────────
    if medium_conf and not did_llm_already and not is_cancelled() and llm_ready():
        entry_lines = []
        for item in medium_conf[:10]:  # cap per cycle
            e = item["entry"]
            entry_lines.append(
                f"epoch={e.epoch} | current_file={item['src_file']} | "
                f"suggested_target={item['target_file']} | "
                f"type={e.entry_type} | tags={','.join(e.tags[:6])} | "
                f"content: {e.content[:250]}"
            )

        prompt = REROUTE_PROMPT.format(
            file_catalog=_build_file_catalog(store),
            entry_list="\n\n".join(entry_lines),
        )

        try:
            from core.llm.client import get_llm_client

            response = await get_llm_client().chat(
                messages=[
                    {"role": "system", "content": "You are a memory file auditor."},
                    {"role": "user", "content": prompt},
                ],
                model=settings.background_model or settings.llm_model,
                max_tokens=1500,
            )
            used_llm = True

            decisions = json.loads(_strip_fence(response.content.strip()))
            if isinstance(decisions, dict):
                decisions = [decisions]

            # Build lookups from candidate list
            epoch_to_src: dict[int, str] = {item["entry"].epoch: item["src_file"] for item in medium_conf}
            _known = await asyncio.to_thread(store.list_files)
            known_files: set[str] = {f.name for f in _known}

            # Count how many entries the LLM wants to send to each proposed new
            # file. A new file is only justified if >=2 entries share it
            # (cluster threshold). Single-entry targets that aren't known files
            # get downgraded to "keep".
            new_file_counts: dict[str, int] = {}
            for dec in decisions:
                if not isinstance(dec, dict) or dec.get("action", "keep").lower() != "move":
                    continue
                target = dec.get("target_file", "")
                if target and target not in known_files:
                    new_file_counts[target] = new_file_counts.get(target, 0) + 1

            for dec in decisions:
                if not isinstance(dec, dict) or dec.get("action", "keep").lower() != "move":
                    continue
                try:
                    epoch = int(dec["epoch"])
                except (KeyError, ValueError, TypeError):
                    continue
                target = dec.get("target_file", "")
                src = epoch_to_src.get(epoch)
                if not src or not target or src == target:
                    continue
                # New file: only allow if it's a genuine cluster (>=2 entries)
                if target not in known_files:
                    if new_file_counts.get(target, 0) < 2:
                        logger.debug(
                            "Snooze: reroute rejected single-entry new file %r (epoch=%d) — no cluster justification",
                            target,
                            epoch,
                        )
                        continue
                    logger.info(
                        "Snooze: reroute creating new file %r (%d entries justify it)",
                        target,
                        new_file_counts[target],
                    )

                if is_cancelled():
                    break
                try:
                    if store.move_entries(src, target, [epoch]):
                        await asyncio.to_thread(archive_entries, store, src, {epoch})
                        rerouted += 1
                        logger.info(
                            "Snooze: LLM rerouted epoch=%d %s → %s (%s)",
                            epoch,
                            src,
                            target,
                            dec.get("reason", ""),
                        )
                        await asyncio.sleep(0.05)
                except Exception as e:
                    logger.warning("Snooze: LLM reroute failed epoch=%d: %s", epoch, e)

        except Exception as e:
            logger.warning("Snooze: reroute LLM call failed: %s", e)

    if rerouted:
        logger.info("Snooze: rerouted %d misplaced entries", rerouted)

    db.set_snooze_state("last_reroute_scan", datetime.now(timezone.utc).isoformat())
    return used_llm, rerouted


# ---------------------------------------------------------------------------
# Activity 4: tag enrichment (no LLM)
# ---------------------------------------------------------------------------

# Capitalized words that are never useful as tags.
_COMMON_WORDS = frozenset(
    {
        "the", "this", "that", "when", "where", "what", "how", "which", "there",
        "here", "with", "from", "into", "upon", "about", "after", "before",
        "during", "between", "through", "error", "note", "found",
    }
)  # fmt: skip


def extract_tags(content: str, existing_tags: list[str]) -> list[str]:
    """Heuristic tag extraction from content. Max 5 new tags per entry."""
    existing_set = {t.lower() for t in existing_tags}
    new_tags = []

    # Technical terms: words with underscores, dots, or camelCase
    for term in re.findall(r"\b([a-z]+_[a-z_]+|[a-z]+\.[a-z.]+|[a-z]+[A-Z][a-zA-Z]+)\b", content):
        t = term.lower()
        if t not in existing_set and len(t) > 3:
            new_tags.append(t)
            existing_set.add(t)

    # Capitalized proper nouns (2+ chars, not at sentence start). Simple
    # heuristic: capitalized words that aren't common English.
    for noun in re.findall(r"(?<=\s)[A-Z][a-z]{2,}", content):
        t = noun.lower()
        if t not in existing_set and t not in _COMMON_WORDS and len(t) > 2:
            new_tags.append(t)
            existing_set.add(t)

    return new_tags[:5]


def update_tags_in_markdown(store, file_name: str, epoch: int, tags: list[str]) -> None:
    """Update or add the @tags comment in markdown for one entry."""

    def _set_tags(content: str) -> str | None:
        epoch_marker = f"<!-- @epoch: {epoch} -->"
        if epoch_marker not in content:
            return None

        new_tags_line = f"<!-- @tags: {','.join(tags)} -->"

        # Find the section for this epoch, then replace its tags line or insert
        # one. maxsplit=1 so a legacy file with a repeated epoch marker keeps
        # everything after the second occurrence instead of losing it.
        parts = content.split(epoch_marker, 1)
        if len(parts) < 2:
            return None

        after = parts[1]
        existing_tags = re.search(r"<!-- @tags:.*?-->", after[:200])
        if existing_tags:
            after = after[: existing_tags.start()] + new_tags_line + after[existing_tags.end() :]
        else:
            after = "\n" + new_tags_line + after

        return parts[0] + epoch_marker + after

    store.rewrite_file(file_name, _set_tags)


async def enrich_tags(store, is_cancelled: Callable[[], bool], *, max_entries: int = 10) -> int:
    """Add heuristic tags to sparsely-tagged entries. Returns entries enriched."""
    from core.memory.format import parse_entries_from_markdown

    if not store:
        return 0

    enriched = 0
    one_hour_ago = int(time.time()) - 3600

    for mem_file in await asyncio.to_thread(store.list_files):
        if is_cancelled() or enriched >= max_entries:
            break

        md_content = await asyncio.to_thread(store.read_file, mem_file.name)
        if not md_content:
            continue

        for entry in parse_entries_from_markdown(mem_file.name, md_content):
            if is_cancelled() or enriched >= max_entries:
                break
            if len(entry.tags) >= 3 or entry.epoch > one_hour_ago:
                continue

            new_tags = extract_tags(entry.content, entry.tags)
            if not new_tags:
                continue

            # Update FTS5 index with enriched tags
            all_tags = list(set(entry.tags + new_tags))[:10]
            conn = store._connect()
            try:
                # FTS5 doesn't support UPDATE — delete and reinsert
                conn.execute(
                    "DELETE FROM memory_fts WHERE file_name = ? AND epoch = ?",
                    (entry.file_name, str(entry.epoch)),
                )
                conn.execute(
                    "INSERT INTO memory_fts "
                    "(file_name, content, tags, entry_type, weight, epoch, source, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.file_name,
                        entry.content,
                        ",".join(all_tags),
                        entry.entry_type,
                        entry.weight,
                        str(entry.epoch),
                        entry.source,
                        str(entry.updated),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            update_tags_in_markdown(store, entry.file_name, entry.epoch, all_tags)
            enriched += 1

    if enriched:
        logger.info("Snooze: enriched tags on %d entries", enriched)
    return enriched


# ---------------------------------------------------------------------------
# Activity 5: index reconciliation (no LLM)
# ---------------------------------------------------------------------------


async def reconcile_index(store, db, is_cancelled: Callable[[], bool]) -> None:
    """Check and fix FTS5 index drift, plus the every-cycle embedding sweep."""
    if not store:
        return

    # Embedding sweep (adaptation plan 1f) runs every cycle, OUTSIDE the
    # 6-hour reconcile throttle — new entries should gain vectors within a
    # cycle of being written, and it's a no-op when nothing is pending.
    if settings.embedding_model and not is_cancelled():
        try:
            from core.llm.embeddings import embed_pending

            await embed_pending(store)
        except Exception as e:
            logger.warning("Snooze: embedding sweep failed: %s", e)

    # Check if reconciliation is due (6 hours)
    last = db.get_snooze_state("last_index_reconcile")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < 21600:  # 6 hours
                return
        except ValueError:
            pass

    if is_cancelled():
        return

    health = await asyncio.to_thread(store.health_check, fix=True)
    db.set_snooze_state("last_index_reconcile", datetime.now(timezone.utc).isoformat())

    if health.get("action") == "reindexed":
        logger.info("Snooze: index reconciliation triggered reindex (%s)", health)
    else:
        logger.debug("Snooze: index in sync (%s)", health)


# ---------------------------------------------------------------------------
# Activity 6: memory file splitting (LLM)
# ---------------------------------------------------------------------------


async def split_file(store, is_cancelled: Callable[[], bool]) -> tuple[bool, int]:
    """Split bloated memory files using LLM-assisted grouping.

    Entries are moved (not duplicated): source entries are archived after the
    corresponding target entries are written. Returns (used_llm, entries_moved)
    — used_llm is True whenever the call was attempted, so the caller can spend
    its maintenance budget even on a failed call.
    """
    from core.memory.format import parse_entries_from_markdown

    if not store:
        return False, 0

    # Find the most bloated file (>= 80 active entries)
    target = None
    _all_files = await asyncio.to_thread(store.list_files)
    for f in sorted(_all_files, key=lambda x: x.entry_count, reverse=True):
        if f.entry_count >= 80:
            target = f
            break

    if not target or is_cancelled():
        return False, 0

    logger.info("Snooze: splitting bloated file %s (%d entries)", target.name, target.entry_count)

    md_content = await asyncio.to_thread(store.read_file, target.name)
    if not md_content:
        return False, 0

    entries = parse_entries_from_markdown(target.name, md_content)
    if len(entries) < 80:
        return False, 0

    # Cap at 150 entries per cycle to keep the LLM prompt manageable;
    # subsequent cycles will continue shrinking the file.
    sample = entries[:150]
    entry_summaries = [f"{i}: [{e.entry_type}] {e.content[:150]}" for i, e in enumerate(sample)]

    _existing = await asyncio.to_thread(store.list_files)
    existing_files = [f.name for f in _existing]

    from core.llm.client import get_llm_client

    prompt = (
        f"These {len(sample)} memory entries are currently all stored in '{target.name}'. "
        f"Re-group them into 2-4 more specific files.\n\n"
        f"EXISTING FILES — prefer routing to these where they fit; "
        f"only propose a NEW name if multiple entries share a coherent topic not covered by any existing file:\n"
        f"{', '.join(existing_files)}\n\n"
        f"Entries:\n" + "\n".join(entry_summaries) + "\n\n"
        f"Rules:\n"
        f"- Every entry must appear in exactly one group.\n"
        f"- New file names must use dot-separated lowercase (e.g., pernix.workers, pernix.automation).\n"
        f"- A small residual group may remain in '{target.name}'.\n\n"
        f'Output JSON only: {{"groups": [{{"file": "name.here", "entries": [0, 1, 5]}}]}} /no_think'
    )

    try:
        response = await get_llm_client().chat(
            messages=[{"role": "user", "content": prompt}],
            model=settings.background_model or settings.llm_model,
            max_tokens=2000,
        )
    except Exception as e:
        logger.warning("Snooze: file split LLM call failed: %s", e)
        return True, 0  # LLM was attempted; count against maintenance budget

    if is_cancelled():
        return True, 0

    # Parse groupings
    try:
        text = response.content.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        groups = json.loads(text.strip()).get("groups", [])
    except (json.JSONDecodeError, KeyError, IndexError):
        logger.warning("Snooze: could not parse file split response")
        return True, 0

    # Build per-target-file epoch lists; deduplicate so each epoch goes to at most one file.
    seen_epochs: set[int] = set()
    epochs_by_file: dict[str, list[int]] = {}
    for group in groups:
        file_name = group.get("file", "")
        indices = group.get("entries", [])
        # Sanitize LLM-generated filename (defense-in-depth)
        file_name = file_name.replace("/", ".").replace("\\", ".").replace("..", ".")
        if not file_name or not indices or file_name == target.name:
            continue
        unique_epochs = []
        for idx in indices:
            if 0 <= idx < len(sample):
                ep = sample[idx].epoch
                if ep not in seen_epochs:
                    seen_epochs.add(ep)
                    unique_epochs.append(ep)
        if unique_epochs:
            epochs_by_file[file_name] = unique_epochs

    if not epochs_by_file:
        return True, 0

    # Move entries: write to target files, then archive in source.
    # move_entries handles FTS indexing and hit-count migration.
    all_moved_epochs: set[int] = set()
    moved = 0
    for file_name, epoch_list in epochs_by_file.items():
        if is_cancelled():
            break
        count = store.move_entries(target.name, file_name, epoch_list)
        if count > 0:
            all_moved_epochs.update(epoch_list)
            moved += count
            logger.debug("Snooze: split %d entries → %s", count, file_name)

    if moved and all_moved_epochs:
        await asyncio.to_thread(archive_entries, store, target.name, all_moved_epochs)
        logger.info(
            "Snooze: split %d entries from %s into %d file(s)",
            moved,
            target.name,
            len(epochs_by_file),
        )

    return True, moved


# ---------------------------------------------------------------------------
# Activity 8: staleness pruning (LLM-gated)
# ---------------------------------------------------------------------------


STALE_PRUNE_PROMPT = """You are a memory curator. These memory entries have low recall rates relative to their age cohort — they are retrieved less often than similar-aged entries.

For each entry, decide:
- KEEP: Still valuable despite low usage (foundational fact, rare but irreplaceable knowledge, identity/preference info)
- PRUNE: Safe to archive (transient context that served its purpose, outdated project state, superseded information)

Err on the side of KEEP when uncertain. Only PRUNE entries that are clearly stale or redundant.

Output a JSON array: [{"epoch": <number>, "verdict": "keep|prune", "reason": "brief reason"}]
Output valid JSON only. No markdown fences. /no_think"""

# Age cohorts, youngest first. Entries younger than the smallest bucket are
# never pruned; bucket selection scans this reversed (an entry lands in the
# oldest bucket it qualifies for).
_COHORT_DAYS = (30, 60, 90, 180, 360)


def _fetch_entries_with_hits(store) -> list:
    """All indexed entries plus their hit counts, oldest first.

    No LIMIT — scans the full FTS table; callers push it off-loop to keep the
    event loop responsive as the store grows.
    """
    c = store._connect()
    try:
        return c.execute("""SELECT f.file_name, f.epoch, f.weight, f.content,
                      COALESCE(h.hit_count, 0) as hit_count
               FROM memory_fts f
               LEFT JOIN memory_hits h
                 ON f.file_name = h.file_name AND f.epoch = h.epoch
               ORDER BY CAST(f.epoch AS INTEGER) ASC""").fetchall()
    finally:
        c.close()


def _stale_candidates(rows, now: int, limit: int = 10) -> list[dict]:
    """Bucket entries by age cohort and return those below their cohort average.

    High-weight entries are exempt (explicitly marked important); zero-hit
    entries 60 days or older qualify regardless of the average.
    """
    cohorts: dict[int, list[dict]] = {d: [] for d in _COHORT_DAYS}
    for row in rows:
        try:
            age_days = (now - int(row["epoch"])) / 86400
        except (ValueError, TypeError):
            continue
        bucket = next((d for d in reversed(_COHORT_DAYS) if age_days >= d), None)
        if bucket is None:
            continue  # < 30 days: never prune
        cohorts[bucket].append(
            {
                "file_name": row["file_name"],
                "epoch": row["epoch"],
                "weight": row["weight"],
                "content": row["content"],
                "hit_count": row["hit_count"],
                "age_days": age_days,
            }
        )

    per_cohort: dict[int, list[dict]] = {}
    for cohort_days, entries in cohorts.items():
        if len(entries) < 3:
            continue  # need enough data for meaningful average
        avg_hits = sum(e["hit_count"] for e in entries) / len(entries)
        qualifying: list[dict] = []
        for entry in entries:
            if entry["weight"] == "high":
                continue
            if entry["hit_count"] < avg_hits or (entry["hit_count"] == 0 and entry["age_days"] >= 60):
                entry["cohort"] = f"{cohort_days}d"
                entry["cohort_avg"] = avg_hits
                qualifying.append(entry)
        if qualifying:
            per_cohort[cohort_days] = qualifying

    # Deal slots round-robin, oldest cohort first. Accumulating every cohort
    # into one list and truncating to `limit` handed every slot to the 30-day
    # cohort — by far the most populous — so the 180- and 360-day cohorts, the
    # entries most likely to actually be stale, were structurally unreachable
    # on any store with more than ~10 under-average young entries. Forgetting
    # was aimed at the wrong end of the age distribution. Round-robin gives
    # every cohort with candidates a slot before any cohort gets a second one;
    # oldest-first order breaks the tie on the final partial round.
    candidates: list[dict] = []
    buckets = [per_cohort[d] for d in reversed(_COHORT_DAYS) if d in per_cohort]
    depth = 0
    while len(candidates) < limit and any(len(b) > depth for b in buckets):
        for bucket in buckets:
            if depth < len(bucket):
                candidates.append(bucket[depth])
                if len(candidates) >= limit:
                    break
        depth += 1
    return candidates


async def prune_stale_entries(store, db, is_cancelled: Callable[[], bool], *, interval_days: int) -> int:
    """Archive low-recall entries using age-cohort analysis + LLM gatekeeper.

    Returns the number of entries archived.
    """
    if not store:
        return 0

    last = db.get_snooze_state("last_stale_prune")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < interval_days * 86400:
                return 0
        except ValueError:
            pass

    if is_cancelled():
        return 0

    rows = await asyncio.to_thread(_fetch_entries_with_hits, store)
    if not rows or len(rows) < 20:
        # Not enough data for meaningful cohort analysis
        db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())
        return 0

    if is_cancelled():
        return 0

    candidates = _stale_candidates(rows, int(time.time()))
    if not candidates:
        db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())
        return 0

    if is_cancelled():
        return 0

    # LLM gatekeeper
    entry_descriptions = [
        f"epoch={c['epoch']} | file={c['file_name']} | "
        f"hits={c['hit_count']} (cohort avg={c['cohort_avg']:.1f}, bucket={c['cohort']}) | "
        f"content: {c['content'][:200]}"
        for c in candidates
    ]

    from core.llm.client import get_llm_client

    try:
        response = await get_llm_client().chat(
            messages=[
                {"role": "system", "content": STALE_PRUNE_PROMPT},
                {"role": "user", "content": "\n\n".join(entry_descriptions)},
            ],
            model=settings.background_model or settings.llm_model,
            max_tokens=1500,
        )
    except Exception as e:
        logger.warning("Snooze: stale prune LLM call failed: %s", e)
        db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())
        return 0

    if is_cancelled():
        return 0

    try:
        verdicts = json.loads(_strip_fence(response.content.strip()))
        if isinstance(verdicts, dict):
            verdicts = [verdicts]
    except json.JSONDecodeError:
        logger.warning("Snooze: could not parse stale prune response")
        db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())
        return 0

    candidate_map = {str(c["epoch"]): c for c in candidates}

    pruned_by_file: dict[str, set[int]] = {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        epoch_str = str(v.get("epoch", ""))
        if v.get("verdict", "keep").lower() != "prune" or epoch_str not in candidate_map:
            continue
        file_name = candidate_map[epoch_str]["file_name"]
        pruned_by_file.setdefault(file_name, set()).add(int(epoch_str))
        logger.debug(
            "Snooze: pruning stale entry epoch=%s file=%s reason=%s", epoch_str, file_name, v.get("reason", "")
        )

    total_pruned = 0
    for file_name, epochs in pruned_by_file.items():
        if is_cancelled():
            break
        await asyncio.to_thread(archive_entries, store, file_name, epochs)
        total_pruned += len(epochs)

    if total_pruned:
        logger.info("Snooze: pruned %d stale entries across %d files", total_pruned, len(pruned_by_file))

    db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())
    return total_pruned
