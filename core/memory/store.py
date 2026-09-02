"""Pernix — Internalized memory store (replaces HyperKB).

Markdown files + SQLite FTS5 index. ~800 lines replacing ~8000.
Markdown files are source of truth; FTS5 index is rebuildable.

Writes are append-by-default (remember, ingest, distill, snooze) with
explicit per-entry mutation via update_entry / delete_entry — used by the
agent's update_memory / forget tools to correct or remove specific entries.

Epoch contract: an epoch is an entry's identity *within its file*, and
content mutation never changes it — update_entry and add_or_supersede_entry
both rewrite in place under the original epoch and stamp @updated. It is not
a durable global identifier: any operation that re-keys an entry into another
file or resolves a collision may bump it (move_entries on target collision,
consolidation's fuse re-keying to the oldest epoch, repair_epoch_collisions
re-epoching legacy duplicates). External references — `[[file@epoch]]`
wiki-links, dream evidence refs, memory_hits rows — therefore survive
corrections but not relocations, and dangling ones are silent by design.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from config import settings
from core.memory.format import (
    MemoryEntry,
    MemoryFile,
    format_entry,
    format_file_header,
    parse_entries_from_markdown,
)
from core.memory.routing import NAMESPACE_KEYWORDS, name_tokens, normalize_file_name
from core.memory.search import SearchResult, search_bm25, search_hybrid, search_recent
from db.database import connect_memory

logger = logging.getLogger("pernix.memory")

__all__ = ["MemoryStore", "NAMESPACE_KEYWORDS", "get_memory_store"]


_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Supersede band — see _supersede_reason.
_SUPERSEDE_RATIO = 0.82
_SUPERSEDE_MAX_DROPPED_TOKENS = 2


def _duplicate_message(dup: SearchResult) -> str:
    """The refusal the dedup gate returns, carrying the supersede hint."""
    preview = dup.entry.content[:160].replace("\n", " ")
    return (
        f"Memory already contains similar content — entry skipped (duplicate of "
        f'{dup.entry.file_name}@{dup.entry.epoch}: "{preview}"). If your version is '
        f"newer or more accurate, supersede it with "
        f"update_memory(file='{dup.entry.file_name}', epoch={dup.entry.epoch}, content=...)."
    )


def _supersede_reason(new_content: str, old_content: str) -> str:
    """Why `new_content` may overwrite `old_content` in place; "" for never.

    Only ever asked about a pair the dedup gate already refused, so "are these
    similar?" is settled. The question here is narrower and much riskier: is
    the new text the *same statement restated* (safe to overwrite) or a near
    neighbour that happens to trip the gate (must not overwrite)?

    Three conditions, all required, all biased toward keeping what is stored:

    1. SequenceMatcher >= 0.82 — the bar the dedup sweep and consolidation
       already require before they are willing to destroy an entry. The write
       gate fires far lower (0.70 ratio OR 0.55 bag-of-words Jaccard over the
       top-3 BM25 hits), and that lower band is full of merely-topical
       neighbours; rewriting one of those replaces a fact with a different fact.
    2. The new text must contribute at least one token the stored text lacks.
       A pure paraphrase or a strict subset has nothing to add, so the stored
       entry — older, possibly already linked to — wins, exactly as today.
    3. The stored text may lose at most two tokens. A correction changes a
       value or two inside an otherwise identical sentence; an entry carrying
       three or more tokens the new version lacks is a distinct fact, and
       overwriting it destroys information. Losing nothing at all is the safe
       case (the new text is a strict superset) and is always allowed.

    Dropping a real correction costs one fact until the next session relearns
    it; overwriting the wrong entry destroys one permanently. When the shape
    is ambiguous this returns "" and the caller falls back to dropping.

    One boundary case is accepted knowingly: a one-word synonym swap has the
    same token shape as a one-value correction, and nothing lexical can tell
    them apart. Such a write rewrites equivalent text with equivalent text —
    no fact is lost, the only cost is an @updated stamp on an entry that was
    already right.
    """
    from core.memory.dedup import content_tokens

    if SequenceMatcher(None, new_content, old_content).ratio() < _SUPERSEDE_RATIO:
        return ""
    new_tokens = content_tokens(new_content)
    old_tokens = content_tokens(old_content)
    if not (new_tokens - old_tokens):
        return ""
    dropped = old_tokens - new_tokens
    if not dropped:
        return "enrichment"
    if len(dropped) <= _SUPERSEDE_MAX_DROPPED_TOKENS:
        return "correction"
    return ""


def _entry_body(section: str) -> str:
    """An entry section minus the merge-bookkeeping headers, for twin detection."""
    return "\n".join(line for line in section.splitlines() if not line.startswith("<!-- @merged_")).strip()


def _notify_oversized_file(md_path) -> None:
    """Tell the user when a memory file is too large to index."""
    try:
        from db import models as _db

        _db.add_notification(
            title="A memory file is too large to index",
            body=(
                f"{md_path.name} is over the 50MB reindex cap, so its entries are absent from "
                "search until it is split or compacted."
            ),
            urgency="normal",
            dedup_key=f"memory-oversized:{md_path.name}",
        )
    except Exception as e:
        logger.debug("Could not raise the oversized-memory notification: %s", e)


def _bucket_matches(file_name: str, space_prefix: str | None) -> bool:
    """Whether a candidate file is in the bucket the caller is writing to.

    With a space active, only that space's files qualify. Without one, only
    global files do — a global session must never be routed into a space,
    whose contents the space's own cascade delete is entitled to destroy.
    """
    from core.memory.routing import space_bucket

    if space_prefix:
        return file_name.startswith(space_prefix)
    return space_bucket(file_name) is None


class MemoryStore:
    """Persistent memory with markdown files + FTS5 search.

    Append-by-default (add_entry) with explicit per-entry mutation
    (update_entry, delete_entry). File-level archival (archive_file) and
    cross-file moves (move_entries) for consolidation.
    """

    def __init__(self, memory_dir: str | None = None):
        self._dir = Path(memory_dir or settings.memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _connect(self):
        return connect_memory()

    def _candor_attest(self, file_name: str, event: str, source: str = "") -> None:
        """Mirror a user-model mutation into the candor add-on, if enabled.

        Fire-and-forget: enqueues onto the bridge's own executor and returns
        immediately, so it is safe from any calling context (these methods run
        both on to_thread workers and, at a few legacy sites, on the event
        loop). Only `user.*` files produce observations; failure never
        propagates into the memory operation.
        """
        if not settings.candor_enabled or not file_name.startswith("user."):
            return
        try:
            from core.extensions.candor.bridge import get_candor_bridge
            from core.extensions.candor.emit import build_memory_observations

            observations = build_memory_observations(
                file_name=file_name, event=event, source=source, ts_ms=int(time.time() * 1000)
            )
            if observations:
                get_candor_bridge().record_nowait(observations)
        except Exception as e:
            logger.debug("Candor attestation skipped for %s: %s", file_name, e)

    def _validate_name(self, name: str) -> str:
        """Sanitize and validate a memory file name. Returns cleaned name."""
        if name.endswith(".md"):
            name = name[:-3]
        if not name:
            raise ValueError("Empty file name")
        if not _NAME_RE.match(name):
            raise ValueError(f"Invalid file name: {name}")
        md_path = (self._dir / f"{name}.md").resolve()
        if not md_path.is_relative_to(self._dir.resolve()):
            raise ValueError(f"Path traversal detected: {name}")
        return name

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def add_entry(
        self,
        content: str,
        file_name: str | None = None,
        entry_type: str = "note",
        tags: str = "",
        weight: str = "normal",
        epoch: int | None = None,
        source: str = "",
        skip_dedup: bool = False,
        origin: str = "",
        space_slug: str | None = None,
    ) -> str:
        """Append an entry to a memory file and index it.

        If file_name is None, auto-routes to best matching file.
        Returns confirmation string.

        skip_dedup: bypass the duplicate gate. For writers whose content is
        by construction similar to entries they are about to supersede
        (consolidation fuse) — the gate would block the write against the
        very entry being replaced.

        space_slug (v33): the writing session's space. Scopes AUTO-routing
        to the space's pernix.space.<slug>.* files; an explicit file_name
        stays a verbatim contract, global names included.
        """
        if not content.strip():
            return "Error: Empty content"

        # Sanitize before dedup/indexing so FTS rows match the stored markdown.
        from core.memory.format import sanitize_entry_content

        content = sanitize_entry_content(content)

        # Only dedup substantive entries — short strings (< 60 chars) have
        # unreliable similarity scores and are allowed through unconditionally.
        if not skip_dedup and len(content) >= 60:
            dup = self.find_duplicate(content)
            if dup is not None:
                # Don't let a newer/corrected fact silently lose to a stale
                # one — point at the match so the caller can supersede it.
                # Automatic writers that can't act on a hint should call
                # add_or_supersede_entry instead of parsing this string.
                return _duplicate_message(dup)

        epoch = epoch or int(time.time())

        # Resolve file name: map to existing file or create new one
        file_name = self._resolve_file_name(file_name, content, space_slug=space_slug)

        file_name = self._validate_name(file_name)

        # Ensure file exists
        self._ensure_file(file_name, content)

        md_path = self._dir / f"{file_name}.md"

        with self._lock:
            # Explicit append to an archived file revives it: drop the header
            # marker so the file (and its prior entries) is live again.
            # Without this, the new entry would be indexed while reindex/
            # health_check still treat the whole file as archived.
            from core.memory.format import is_file_archived

            revived = False
            raw = md_path.read_text(encoding="utf-8")
            if is_file_archived(raw):
                raw = raw.replace("\n<!-- @archived: true -->", "", 1)
                md_path.write_text(raw, encoding="utf-8")
                revived = True
                logger.info("Revived archived memory file on append: %s", file_name)

            # File lock + DB commit must be atomic to prevent index/markdown drift.
            # Keep fcntl lock held until DB commit completes.
            with open(md_path, "a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    tag_list = tags if tags else ",".join(self._infer_tags(content, file_name))
                    conn = self._connect()
                    try:
                        # Epochs double as entry identity within a file — bump
                        # until unique so update/delete can't hit two entries
                        # (same strategy move_entries uses across files).
                        while conn.execute(
                            "SELECT 1 FROM memory_fts WHERE file_name = ? AND epoch = ?",
                            (file_name, str(epoch)),
                        ).fetchone():
                            epoch += 1

                        # tag_list, not tags: inferred tags must reach the
                        # markdown too, or the next reindex() erases them
                        # (markdown is source of truth).
                        formatted = format_entry(
                            content, entry_type, tag_list, weight, source=source, epoch=epoch, origin=origin
                        )
                        f.write(formatted)
                        f.flush()

                        if revived:
                            # Restore the revived file's prior entries alongside
                            # the new one.
                            self._reindex_file(conn, file_name, raw + formatted)
                        else:
                            conn.execute(
                                "INSERT INTO memory_fts "
                                "(file_name, content, tags, entry_type, weight, epoch, source, updated) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (file_name, content, tag_list, entry_type, weight, str(epoch), source, "0"),
                            )
                            conn.execute(
                                "UPDATE memory_files SET entry_count = entry_count + 1, updated_at = ? WHERE name = ?",
                                (epoch, file_name),
                            )
                        conn.commit()
                    finally:
                        conn.close()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        self._candor_attest(file_name, "attest", source)
        return f"Saved to {file_name} (epoch={epoch})"

    def add_or_supersede_entry(
        self,
        content: str,
        file_name: str | None = None,
        entry_type: str = "note",
        tags: str = "",
        weight: str = "normal",
        epoch: int | None = None,
        source: str = "",
        origin: str = "",
        space_slug: str | None = None,
    ) -> str:
        """add_entry, except a blocked duplicate that is a *correction* rewrites it.

        The dedup gate refuses any write similar to something already stored —
        which is precisely the shape of a corrected fact. add_entry hands back
        a supersede hint naming the blocking entry, but only the agent-facing
        `remember` tool has anything that can read it: the automatic writers
        (distill, ingest) dropped every correction they learned, and the stale
        version survived. This path gives clear corrections somewhere to go.

        When the blocking entry is the same statement restated (see
        _supersede_reason) the new text replaces it via update_entry — original
        epoch preserved, @updated stamped, so wiki-links keep resolving and
        recall shows the correction date. Everything else is unchanged: novel
        content is appended and an ambiguous duplicate is still refused with
        the same message add_entry returns.
        """
        if not content.strip():
            return "Error: Empty content"

        from core.memory.format import sanitize_entry_content

        content = sanitize_entry_content(content)

        # Same 60-char floor as add_entry: below it similarity is unreliable,
        # so there is nothing trustworthy enough to overwrite an entry on.
        if len(content) >= 60:
            dup = self.find_duplicate(content)
            if dup is not None:
                reason = _supersede_reason(content, dup.entry.content)
                if not reason:
                    return _duplicate_message(dup)
                result = self.update_entry(dup.entry.file_name, dup.entry.epoch, content)
                if result.startswith("Error"):
                    # Couldn't rewrite safely (epoch collision, file gone) —
                    # fall back to the gate's original refusal rather than
                    # appending a near-copy alongside the entry we meant to fix.
                    logger.warning(
                        "Supersede of %s@%s failed, keeping stored entry: %s",
                        dup.entry.file_name,
                        dup.entry.epoch,
                        result,
                    )
                    return _duplicate_message(dup)
                logger.info(
                    "Superseded %s@%s (%s, source=%s)",
                    dup.entry.file_name,
                    dup.entry.epoch,
                    reason,
                    source or "unknown",
                )
                return f"Superseded {dup.entry.file_name}@{dup.entry.epoch} ({reason})"

        # The gate already ran (or the content is too short to judge) — running
        # it again in add_entry would only pay for the same search twice.
        return self.add_entry(
            content,
            file_name=file_name,
            entry_type=entry_type,
            tags=tags,
            weight=weight,
            epoch=epoch,
            source=source,
            skip_dedup=True,
            origin=origin,
            space_slug=space_slug,
        )

    # Canonical implementations live in core.memory.routing (shared with
    # consolidation clustering); kept as static methods for callers/tests.
    _normalize_name = staticmethod(normalize_file_name)
    _name_tokens = staticmethod(name_tokens)

    def _resolve_file_name(self, suggested: str | None, content: str, space_slug: str | None = None) -> str:
        """Map a suggested file name to an existing file when possible.

        Resolution cascade for an explicit suggestion:
        1. Exact normalized-name match — against ALL known files, empty and
           archived ones included (a file that exists with zero entries must
           be able to receive its first entry by name, and an explicit write
           to an archived file flows to add_entry's revive path). The old
           entry_count>0 filter made an explicitly named empty file
           invisible here, and the content-dominance step then hijacked the
           write — the curiosity drive's pernix.findings ledger sat at 0
           entries while 4 runs' findings were silently diverted to a
           look-alike file.
        2. Token-Jaccard >= 0.6 against files that hold entries.
        3. Honor the suggestion. Content-based routing decides only when NO
           file was named (_auto_route) — a valid explicit target is a
           contract, not a hint, and content dominance is self-reinforcing:
           every mis-route makes the wrong file more dominant for exactly
           this content (the gravity-well effect _auto_route guards against).
        """
        if not suggested:
            return self._auto_route(content, space_slug=space_slug)

        # Clean the suggested name through validation-safe form
        try:
            suggested = self._validate_name(suggested)
        except ValueError:
            return self._auto_route(content, space_slug=space_slug)

        # Build map of known files (normalized → actual name), tracking
        # which ones actually hold entries.
        conn = self._connect()
        try:
            rows = conn.execute("SELECT name, entry_count FROM memory_files").fetchall()
        finally:
            conn.close()
        existing = {r["name"]: self._normalize_name(r["name"]) for r in rows}
        populated = {r["name"] for r in rows if (r["entry_count"] or 0) > 0}

        if not existing:
            return suggested

        suggested_norm = self._normalize_name(suggested)
        suggested_tokens = self._name_tokens(suggested)

        # 1. Exact normalized match (empty and archived files included —
        # add_entry revives an archived file on explicit append)
        for actual, norm in existing.items():
            if norm == suggested_norm:
                return actual

        # 2. Token Jaccard >= 0.6 against populated files only. Space-bucket
        # guard (v33): "pernix.space.alpha.research" is 0.6-similar to
        # "pernix.space.beta.research" — without the bucket check an explicit
        # write to one space silently lands in another (or a space name maps
        # onto a global file and vice versa).
        from core.memory.routing import space_bucket as _space_bucket

        suggested_bucket = _space_bucket(suggested)
        best_jaccard = 0.0
        best_match = None
        for actual in populated:
            if _space_bucket(actual) != suggested_bucket:
                continue
            actual_tokens = self._name_tokens(actual)
            if not suggested_tokens or not actual_tokens:
                continue
            jaccard = len(suggested_tokens & actual_tokens) / len(suggested_tokens | actual_tokens)
            if jaccard > best_jaccard:
                best_jaccard = jaccard
                best_match = actual
        if best_jaccard >= 0.6 and best_match:
            return best_match

        # 3. Honor the explicit suggestion — creates the file if needed.
        return suggested

    def _auto_route(self, content: str, space_slug: str | None = None) -> str:
        """Find best existing file for content, or suggest new one.

        Always evaluates ALL signals (FTS5, namespace keywords, file metadata)
        and combines them. No early returns — prevents gravity-well effect
        where one large file attracts all new entries.

        space_slug (v33): route inside the space's bucket. Namespace hits map
        to pernix.space.<slug>.<topic>, candidate files are restricted to the
        space prefix, and the fallback is the space's own notes file — an
        auto-routed write from a space session never lands in a global file
        (or another space's).
        """
        space_prefix = f"pernix.space.{space_slug}." if space_slug else None
        content_lower = content.lower()
        # Candidates: {file_name: score}
        candidates: dict[str, float] = {}

        def _scoped(name: str) -> str:
            """Map a canonical bucket into the space (pernix.research ->
            pernix.space.<slug>.research); non-space routing is identity."""
            if not space_prefix:
                return name
            return space_prefix + name.rsplit(".", 1)[-1]

        # Signal 1: Namespace keyword matching (always runs, cheap)
        for ns, keywords in NAMESPACE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in content_lower)
            if score > 0:
                # Boost namespace matches — these are curated topical buckets
                ns_target = _scoped(ns)
                candidates[ns_target] = candidates.get(ns_target, 0) + score * 2.0

        # Signal 2: Existing file metadata keyword overlap
        conn = self._connect()
        try:
            rows = conn.execute("SELECT name, keywords FROM memory_files WHERE entry_count > 0").fetchall()
            file_count = len(rows)
            for row in rows:
                # The bucket boundary cuts BOTH ways. Filtering only when a
                # space is active let a global remember() land in a space's
                # file (space files are the content-richest on a busy space,
                # so they win keyword overlap), and the space cascade delete
                # then destroyed a memory that never belonged to it.
                if not _bucket_matches(row["name"], space_prefix):
                    continue
                file_kws = set(row["keywords"].lower().split(","))
                content_words = set(content_lower.split())
                overlap = len(file_kws & content_words)
                if overlap > 0:
                    candidates[row["name"]] = candidates.get(row["name"], 0) + overlap
        finally:
            conn.close()

        # Signal 3: FTS5 content similarity (only when enough files exist)
        # During cold-start (< 5 files), FTS5 creates gravity wells — rely on
        # keyword routing instead. Once there are enough files, FTS5 provides
        # meaningful differentiation.
        if file_count >= 5:
            try:
                conn = self._connect()
                try:
                    results = search_bm25(conn, content[:200], limit=10, file_prefix=space_prefix)
                finally:
                    conn.close()
                if results:
                    file_scores: dict[str, float] = {}
                    for r in results:
                        fn = r.entry.file_name
                        # search_bm25's file_prefix only constrains the
                        # in-space direction; a global write still had every
                        # space file as a candidate here.
                        if not _bucket_matches(fn, space_prefix):
                            continue
                        file_scores[fn] = file_scores.get(fn, 0) + r.score
                    for fn, score in file_scores.items():
                        # Require strong FTS5 signal (>= 3.0) to influence routing
                        if score >= 3.0:
                            candidates[fn] = candidates.get(fn, 0) + score
            except Exception:
                pass

        if candidates:
            return max(candidates, key=candidates.get)
        return space_prefix + "notes" if space_prefix else "pernix.notes"

    def _ensure_file(self, file_name: str, content: str = "") -> None:
        """Create memory file if it doesn't exist."""
        file_name = self._validate_name(file_name)
        md_path = self._dir / f"{file_name}.md"
        if md_path.exists():
            return

        # Derive metadata from filename
        parts = file_name.replace(".", " ").replace("-", " ").replace("_", " ").split()
        description = " ".join(parts).title()
        keywords = [p.lower() for p in parts if len(p) > 2]

        # Write file header
        md_path.parent.mkdir(parents=True, exist_ok=True)
        header = format_file_header(file_name, description, keywords)
        md_path.write_text(header)

        # Register in DB
        epoch = int(time.time())
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO memory_files (name, description, keywords, entry_count, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (file_name, description, ",".join(keywords), epoch, epoch),
            )
            conn.commit()
        finally:
            conn.close()

        logger.info("Created memory file: %s", file_name)

    def _infer_tags(self, content: str, file_name: str) -> list[str]:
        """Infer tags from content and file name."""
        tags = set()
        # Add date tag
        tags.add(time.strftime("%Y-%m-%d"))
        # Add file name segments
        for part in file_name.replace(".", " ").split():
            if len(part) > 2:
                tags.add(part.lower())
        return list(tags)[:10]

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def find_duplicate(self, content: str, threshold: float = 0.70) -> SearchResult | None:
        """Multi-signal dedup check. Returns the matched existing entry, or None.

        An exact-equality lookup runs FIRST, straight against the index table,
        because the similarity gate below is only as good as search RANKING:
        it inspects the top-3 candidates, and in a file full of near-identical
        entries (dozens of market snapshots differing only in numbers) the
        byte-identical twin can rank fourth. That is not hypothetical — the
        live box accumulated 409 redundant exact copies (371 from distill,
        epochs seconds apart) with this gate in place, and dream flagged three
        of them as a data-ingestion bug before we measured the rest. The
        equality check is immune to ranking, to the 0.70 threshold, and to
        embedding availability (the hybrid channel degrades when the embed
        endpoint is down, which the box also logged the same day).
        """
        exact = self._find_exact(content)
        if exact is not None:
            return exact
        candidates = self.search(content, limit=3, _track_hits=False)
        if not candidates:
            return None

        content_words = set(content.lower().split())

        for r in candidates:
            # Signal 1: SequenceMatcher
            sim = SequenceMatcher(None, content, r.entry.content).ratio()
            if sim > threshold:
                return r

            # Signal 2: bag-of-words Jaccard
            existing_words = set(r.entry.content.lower().split())
            if len(content_words) > 3 and len(existing_words) > 3:
                jaccard = len(content_words & existing_words) / len(content_words | existing_words)
                if jaccard > 0.55:
                    return r

        return None

    def is_duplicate(self, content: str, threshold: float = 0.70) -> bool:
        """True if content duplicates an existing entry (see find_duplicate)."""
        return self.find_duplicate(content, threshold) is not None

    def _find_exact(self, content: str) -> SearchResult | None:
        """Byte-exact content match against the index. Ranking-independent.

        Byte equality is the deliberate scope: callers sanitize before the
        gate and the index stores sanitized content, so a writer repeating
        itself produces byte-identical rows — the observed failure. Near-
        duplicates with cosmetic edits remain the similarity gate's job.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT file_name, content, epoch, entry_type, weight, source "
                "FROM memory_fts WHERE content = ? LIMIT 1",
                (content,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        entry = MemoryEntry(
            file_name=row[0],
            content=row[1],
            epoch=int(row[2]),
            entry_type=row[3] or "note",
            weight=row[4] or "normal",
            source=row[5] or "",
        )
        return SearchResult(entry=entry, score=1.0, source="bm25")

    # ------------------------------------------------------------------
    # Entry-level mutations (update / delete)
    # ------------------------------------------------------------------

    def _reindex_file(self, conn, file_name: str, new_raw: str) -> None:
        """Rebuild FTS5 index for a file from updated raw markdown content.

        Deletes all existing rows for the file and re-inserts from parsed entries.
        Uses a file-level delete because FTS5 compound WHERE on UNINDEXED columns
        is unreliable — only equality on FTS-indexed columns or rowid is safe.
        entry_count is set to the parsed entry count (absolute, not relative).
        """
        from core.memory.format import parse_entries_from_markdown

        conn.execute("DELETE FROM memory_fts WHERE file_name = ?", (file_name,))
        entries = parse_entries_from_markdown(file_name, new_raw)
        for e in entries:
            tag_str = ",".join(e.tags) if e.tags else ""
            conn.execute(
                "INSERT INTO memory_fts "
                "(file_name, content, tags, entry_type, weight, epoch, source, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (file_name, e.content, tag_str, e.entry_type, e.weight, str(e.epoch), e.source, str(e.updated)),
            )
        now = int(time.time())
        conn.execute(
            "UPDATE memory_files SET entry_count = ?, updated_at = ? WHERE name = ?",
            (len(entries), now, file_name),
        )

    def _reindex_commit(self, file_name: str, raw: str) -> None:
        """Rebuild one file's FTS rows from raw markdown and commit."""
        conn = self._connect()
        try:
            self._reindex_file(conn, file_name, raw)
            conn.commit()
        finally:
            conn.close()

    def _write_locked(self, md_path: Path, data: str, on_written: Callable[[], None] | None = None) -> None:
        """Replace a file's contents atomically, with writers excluded throughout.

        The markdown IS the source of truth — the index is derived and can be
        rebuilt from it, never the other way round. So the file must never be
        observable in a half-written state. Writing in place (seek/truncate/
        write) is not crash-safe however tightly it is locked: a kill or power
        loss between the truncate and the completed write leaves the file empty
        or partial, and the next health_check(fix=True) rebuilds the index from
        the wreckage, which turns a recoverable interruption into permanent
        data loss.

        Write to a sibling temp file, fsync it, then os.replace() onto the
        target: rename is atomic, so a reader sees either the whole old file
        or the whole new one and never an empty one. That also retires the
        flock-after-truncate hazard this method used to work around — readers
        take no lock at all and no longer need one.

        The flock on the target is now purely writer-writer exclusion, and it
        is held across `on_written` so an index update commits while no other
        writer can touch the markdown. It is advisory and, after the replace,
        refers to the old inode — in-process writers are already serialized by
        self._lock (the caller holds it), so the residual window is a second
        OS process writing the same file during the index commit, which this
        single-process deployment does not do.
        """
        tmp_path = md_path.with_name(md_path.name + ".tmp")
        fd = os.open(md_path, os.O_RDWR | os.O_CREAT, 0o644)
        with os.fdopen(fd, "r+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                with open(tmp_path, "w", encoding="utf-8") as tmp:
                    tmp.write(data)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_path, md_path)
                if on_written is not None:
                    on_written()
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def rewrite_file(self, file_name: str, transform: Callable[[str], str | None]) -> bool:
        """Read a memory file, apply `transform` to its raw markdown, write it back.

        The store's own mutations and the maintenance sweeps both need
        read-modify-write on a memory file. The sweeps reimplemented it against
        `store._lock` / `store._dir` because the primitive wasn't on the public
        surface — which is how the flock-after-truncate bug got copied into
        them. This is the one implementation both use.

        Returns True when the file was rewritten; False when it doesn't exist
        or `transform` returned None / unchanged text.
        """
        file_name = self._validate_name(file_name)
        md_path = self._dir / f"{file_name}.md"
        if not md_path.exists():
            return False
        with self._lock:
            raw = md_path.read_text(encoding="utf-8")
            new_raw = transform(raw)
            if new_raw is None or new_raw == raw:
                return False
            self._write_locked(md_path, new_raw)
        return True

    def update_entry(self, file_name: str, epoch: int, new_content: str) -> str:
        """Replace the content of an existing entry (identified by epoch).

        Preserves all metadata (type, tags, weight, source). Syncs FTS5 index.
        Returns a confirmation string or an error string.
        """
        file_name = self._validate_name(file_name)
        md_path = self._dir / f"{file_name}.md"
        if not md_path.exists():
            return f"Error: memory file '{file_name}' not found"
        if not new_content.strip():
            return "Error: new content must not be empty"

        from core.memory.format import sanitize_entry_content

        new_content = sanitize_entry_content(new_content)

        epoch_marker = f"<!-- @epoch: {epoch} -->"

        with self._lock:
            raw = md_path.read_text(encoding="utf-8")
            sections = raw.split("\n---\n")

            matches = sum(1 for s in sections if epoch_marker in s)
            if matches > 1:
                return (
                    f"Error: {matches} entries in '{file_name}' share epoch={epoch} "
                    "(legacy collision); run memory maintenance to repair, then retry"
                )

            found = False
            new_sections = []
            for section in sections:
                if epoch_marker in section:
                    found = True
                    # Preserve all HTML comment metadata lines exactly as-is,
                    # replace only the content (non-comment) lines. Stamp
                    # @updated so recall can present the correction date
                    # instead of making the refreshed fact look old.
                    meta_lines = [
                        ln for ln in section.split("\n") if ln.strip().startswith("<!--") and "@updated:" not in ln
                    ]
                    meta_lines.append(f"<!-- @updated: {int(time.time())} -->")
                    new_sections.append("\n".join(meta_lines) + "\n" + new_content + "\n")
                else:
                    new_sections.append(section)

            if not found:
                return f"Error: no entry with epoch={epoch} in '{file_name}'"

            new_raw = "\n---\n".join(new_sections)

            self._write_locked(md_path, new_raw, on_written=lambda: self._reindex_commit(file_name, new_raw))

        logger.info("Updated memory entry epoch=%d in '%s'", epoch, file_name)
        self._candor_attest(file_name, "revise")
        return f"Updated entry epoch={epoch} in '{file_name}'"

    def delete_entry(self, file_name: str, epoch: int) -> str:
        """Remove an entry (identified by epoch) from its file and FTS5 index.

        Returns a confirmation string or an error string.
        """
        file_name = self._validate_name(file_name)
        md_path = self._dir / f"{file_name}.md"
        if not md_path.exists():
            return f"Error: memory file '{file_name}' not found"

        epoch_marker = f"<!-- @epoch: {epoch} -->"

        with self._lock:
            raw = md_path.read_text(encoding="utf-8")
            sections = raw.split("\n---\n")

            matches = sum(1 for s in sections if epoch_marker in s)
            if matches > 1:
                return (
                    f"Error: {matches} entries in '{file_name}' share epoch={epoch} "
                    "(legacy collision); run memory maintenance to repair, then retry"
                )

            found = False
            new_sections = []
            for section in sections:
                if epoch_marker in section:
                    found = True  # drop this section
                else:
                    new_sections.append(section)

            if not found:
                return f"Error: no entry with epoch={epoch} in '{file_name}'"

            new_raw = "\n---\n".join(new_sections)

            self._write_locked(md_path, new_raw, on_written=lambda: self._reindex_commit(file_name, new_raw))

        logger.info("Deleted memory entry epoch=%d from '%s'", epoch, file_name)
        self._candor_attest(file_name, "forget")
        return f"Deleted entry epoch={epoch} from '{file_name}'"

    # ------------------------------------------------------------------
    # Archive & merge operations (used by consolidation)
    # ------------------------------------------------------------------

    def archive_file(self, name: str) -> None:
        """Mark entire file as archived: tag header, remove FTS5 entries, zero count."""
        name = self._validate_name(name)
        md_path = self._dir / f"{name}.md"
        if not md_path.exists():
            return

        with self._lock:
            content = md_path.read_text()
            # Add archived tag after file header if not already present
            if "<!-- @archived: true -->" not in content:
                # Insert after the @created line
                content = content.replace(
                    f"<!-- @file: {name} -->",
                    f"<!-- @file: {name} -->\n<!-- @archived: true -->",
                )
                self._write_locked(md_path, content)

            # Remove all FTS5 entries and zero count
            conn = self._connect()
            try:
                conn.execute("DELETE FROM memory_fts WHERE file_name = ?", (name,))
                conn.execute("UPDATE memory_files SET entry_count = 0 WHERE name = ?", (name,))
                conn.commit()
            finally:
                conn.close()

        logger.info("Archived memory file: %s", name)

    def delete_file(self, name: str) -> bool:
        """Hard-delete a memory file: markdown, FTS rows, registry row, hit
        counters and vectors. Unlike archive_file this is irreversible —
        used by space cascade-delete, where the user explicitly opted in.
        Returns True when a markdown file was actually removed."""
        name = self._validate_name(name)
        md_path = self._dir / f"{name}.md"
        with self._lock:
            existed = md_path.exists()
            if existed:
                md_path.unlink()
            conn = self._connect()
            try:
                conn.execute("DELETE FROM memory_fts WHERE file_name = ?", (name,))
                conn.execute("DELETE FROM memory_files WHERE name = ?", (name,))
                conn.execute("DELETE FROM memory_hits WHERE file_name = ?", (name,))
                conn.execute("DELETE FROM vectors WHERE file_name = ?", (name,))
                conn.commit()
            finally:
                conn.close()
        if existed:
            logger.info("Deleted memory file: %s", name)
        return existed

    def move_entries(
        self,
        source_file: str,
        target_file: str,
        epochs: list[int],
    ) -> int:
        """Copy entries from source to target preserving epochs.

        Adds @merged_from metadata. Migrates hit counts.
        Does NOT archive source entries — caller handles that.
        Returns count of entries moved.
        """
        source_file = self._validate_name(source_file)
        target_file = self._validate_name(target_file)

        # Parse source entries
        md_content = self.read_file(source_file)
        if not md_content:
            return 0

        entries = parse_entries_from_markdown(source_file, md_content)
        epoch_set = set(epochs)
        to_move = [e for e in entries if e.epoch in epoch_set]

        if not to_move:
            return 0

        # Ensure target exists
        self._ensure_file(target_file)

        moved = 0
        conn = self._connect()
        try:
            for entry in to_move:
                # Check for epoch collision in target; loop until clear
                actual_epoch = entry.epoch
                while conn.execute(
                    "SELECT epoch FROM memory_fts WHERE file_name = ? AND epoch = ?",
                    (target_file, str(actual_epoch)),
                ).fetchone():
                    actual_epoch += 1

                # Format and append to target markdown
                formatted = format_entry(
                    entry.content,
                    entry.entry_type,
                    ",".join(entry.tags),
                    entry.weight,
                    source=entry.source,
                    epoch=actual_epoch,
                    merged_from=source_file,
                    updated=entry.updated,
                    origin=entry.origin,
                )
                md_path = self._dir / f"{target_file}.md"
                with self._lock:
                    with open(md_path, "a") as f:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                        f.write(formatted)
                        f.flush()
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

                # Index in FTS5
                tag_list = ",".join(entry.tags)
                conn.execute(
                    "INSERT INTO memory_fts "
                    "(file_name, content, tags, entry_type, weight, epoch, source, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        target_file,
                        entry.content,
                        tag_list,
                        entry.entry_type,
                        entry.weight,
                        str(actual_epoch),
                        entry.source,
                        str(entry.updated),
                    ),
                )

                # Migrate hit counts
                hit_row = conn.execute(
                    "SELECT hit_count, last_hit_at FROM memory_hits WHERE file_name = ? AND epoch = ?",
                    (source_file, str(entry.epoch)),
                ).fetchone()
                if hit_row:
                    conn.execute(
                        "INSERT INTO memory_hits (file_name, epoch, hit_count, last_hit_at) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(file_name, epoch) DO UPDATE SET "
                        "hit_count = hit_count + ?, last_hit_at = MAX(last_hit_at, ?)",
                        (
                            target_file,
                            str(actual_epoch),
                            hit_row["hit_count"],
                            hit_row["last_hit_at"],
                            hit_row["hit_count"],
                            hit_row["last_hit_at"],
                        ),
                    )
                    conn.execute(
                        "DELETE FROM memory_hits WHERE file_name = ? AND epoch = ?",
                        (source_file, str(entry.epoch)),
                    )

                moved += 1

            # Update both file counts atomically
            if moved > 0:
                now = int(time.time())
                conn.execute(
                    "UPDATE memory_files SET entry_count = entry_count + ?, updated_at = ? WHERE name = ?",
                    (moved, now, target_file),
                )
                conn.execute(
                    "UPDATE memory_files SET entry_count = MAX(0, entry_count - ?), updated_at = ? WHERE name = ?",
                    (moved, now, source_file),
                )
            conn.commit()
        finally:
            conn.close()

        logger.info("Moved %d entries from %s to %s", moved, source_file, target_file)
        return moved

    # ------------------------------------------------------------------
    # Search operations
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        mode: str = "hybrid",
        limit: int = 10,
        after_epoch: int | None = None,
        _track_hits: bool = True,
        expand_wikilinks: bool = False,
        space_slug: str | None = None,
    ) -> list[SearchResult]:
        """Search memory entries.

        expand_wikilinks: one-hop [[file-name]]/[[file@epoch]] expansion
        (H4, plan §12.5) — linked entries append with source="link".

        space_slug (v33): prioritize the space's pernix.space.<slug>.* files.
        Implemented as a second, prefix-restricted query merged in front of
        the global results — scores are NEVER mutated (the documented scale
        contract, search.py: > 3.0 strong · 1.0–3.0 weak · < 1.0 noise,
        must keep holding), so a space hit is promoted by ORDER only. One
        guard: a space hit under the 1.0 noise floor sinks below every real
        global hit instead of displacing one.
        """
        conn = self._connect()
        try:
            if mode == "bm25":
                results = search_bm25(conn, query, limit=limit, after_epoch=after_epoch)
            elif mode == "recent":
                results = search_recent(conn, limit=limit)
            else:
                results = search_hybrid(conn, query, limit=limit, after_epoch=after_epoch)

            if space_slug and mode != "recent":
                results = self._merge_space_first(conn, query, results, mode, limit, after_epoch, space_slug)

            if expand_wikilinks and results:
                from core.memory.search import expand_links

                results = expand_links(conn, results)
        finally:
            conn.close()

        if _track_hits and results:
            self._record_hits(results)
        return results

    @staticmethod
    def _merge_space_first(conn, query, global_results, mode, limit, after_epoch, space_slug):
        """Order-only space prioritization for search() — see its docstring."""
        from core.memory.routing import SPACE_PREFIX_FMT

        prefix = SPACE_PREFIX_FMT.format(slug=space_slug)
        if mode == "bm25":
            space_results = search_bm25(conn, query, limit=limit, after_epoch=after_epoch, file_prefix=prefix)
        else:
            space_results = search_hybrid(conn, query, limit=limit, after_epoch=after_epoch, file_prefix=prefix)

        space_results.sort(key=lambda r: r.score, reverse=True)
        # Noise-floor guard: space hits at documented-noise scores (< 1.0)
        # sink below real global hits — but only when real globals EXIST.
        # When everything scored as noise (tiny corpus, short query), space
        # hits still lead: there is nothing better to protect.
        has_real_global = any(r.score >= 1.0 for r in global_results)
        if has_real_global:
            leading = [r for r in space_results if r.score >= 1.0]
            space_noise = [r for r in space_results if r.score < 1.0]
        else:
            leading, space_noise = space_results, []

        merged: list = []
        seen: set[tuple] = set()
        for r in leading + list(global_results) + space_noise:
            key = (r.entry.file_name, r.entry.epoch)
            if key not in seen:
                seen.add(key)
                merged.append(r)
            if len(merged) >= limit:
                break
        return merged

    def _record_hits(self, results: list[SearchResult]) -> None:
        """Record usage hits for search results. Non-blocking, never raises."""
        try:
            now = int(time.time())
            conn = self._connect()
            try:
                for r in results:
                    conn.execute(
                        "INSERT INTO memory_hits (file_name, epoch, hit_count, last_hit_at) "
                        "VALUES (?, ?, 1, ?) "
                        "ON CONFLICT(file_name, epoch) DO UPDATE SET "
                        "hit_count = hit_count + 1, last_hit_at = ?",
                        (r.entry.file_name, str(r.entry.epoch), now, now),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.debug("Failed to record memory hits: %s", e)

    def search_lessons(
        self, query: str, limit: int = 10, _track_hits: bool = True, space_slug: str | None = None
    ) -> list[SearchResult]:
        """Search lesson-type memory entries with age-based decay.

        Lessons are operational workarounds extracted by the refine pass from
        failed sessions. Tagged `lesson` and stored with entry_type='lesson'.

        Age decay: lessons reference a code state at the time they were
        written. The codebase moves on; a lesson that names a "manifest bug"
        from a past run-that-was-since-fixed misleads new scouts into manual
        intervention against a manifest that's now correct. We apply a
        multiplicative decay (1.0 at <14d, 0.5 at <60d, 0.25 at <180d, 0.05
        beyond). Lessons that survive after decay still show up — they're
        just outranked by anything fresher and roughly comparable.
        """
        results = self.search(query, mode="hybrid", limit=limit * 4, _track_hits=False, space_slug=space_slug)
        lessons = [r for r in results if r.entry.entry_type == "lesson"]
        if not lessons:
            return []

        now_ts = int(time.time())
        decayed: list[SearchResult] = []
        for r in lessons:
            age_days = max(0, (now_ts - int(r.entry.epoch or now_ts)) / 86400.0)
            if age_days < 14:
                factor = 1.0
            elif age_days < 60:
                factor = 0.5
            elif age_days < 180:
                factor = 0.25
            else:
                factor = 0.05
            # SearchResult is a dataclass — replace the score field.
            from dataclasses import replace as _replace

            decayed.append(_replace(r, score=r.score * factor))

        # Re-sort by decayed score so old lessons sink behind fresher peers.
        decayed.sort(key=lambda r: r.score, reverse=True)
        out = decayed[:limit]
        if out and _track_hits:
            self._record_hits(out)
        return out

    def recall(self, query: str, top: int = 5, min_score: float = 0.0) -> str:
        """Search and format results for display."""
        results = self.search(query, limit=top)
        if min_score > 0:
            results = [r for r in results if r.score >= min_score]
        if not results:
            return ""
        lines = []
        for r in results:
            lines.append(f"[{r.entry.file_name} score={r.score:.1f}] {r.entry.content[:800]}")
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def list_files(self) -> list[MemoryFile]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM memory_files ORDER BY updated_at DESC").fetchall()
            return [
                MemoryFile(
                    name=r["name"],
                    description=r["description"],
                    keywords=[k.strip() for k in r["keywords"].split(",")],
                    entry_count=r["entry_count"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def get_entry(self, file_name: str, epoch: int) -> MemoryEntry | None:
        """The live entry at (file_name, epoch), or None when it isn't there.

        Reads the markdown, not the FTS index: the memory tools use this to
        prove a write actually landed in the source of truth, and an index row
        the reindex would later drop is not proof. Read-only — never mutates,
        never raises on a bad name or a missing file.
        """
        try:
            file_name = self._validate_name(file_name)
        except ValueError:
            return None
        md_path = self._dir / f"{file_name}.md"
        if not md_path.exists():
            return None
        for entry in parse_entries_from_markdown(file_name, md_path.read_text(encoding="utf-8")):
            if entry.epoch == epoch:
                return entry
        return None

    def read_file(self, name: str) -> str | None:
        name = self._validate_name(name)
        md_path = self._dir / f"{name}.md"
        if not md_path.exists():
            return None
        return md_path.read_text()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Semantic-retrieval sidecar (adaptation plan 1f)
    # ------------------------------------------------------------------

    def pending_embeddings(self, limit: int = 256) -> list[dict]:
        """Entries whose vector is missing, model-mismatched, or content-stale.

        Staleness is judged in Python against the stored content_hash — the
        markdown is truth and the vector must describe the current text.
        """
        from core.llm.embeddings import active_model
        from core.llm.embeddings import content_hash as _hash

        model = active_model()
        if not model:
            return []
        with self._lock:
            conn = self._connect()
            try:
                existing = {
                    (r["file_name"], str(r["epoch"])): (r["model"], r["content_hash"])
                    for r in conn.execute("SELECT file_name, epoch, model, content_hash FROM vectors")
                }
                pending: list[dict] = []
                for row in conn.execute("SELECT file_name, epoch, content FROM memory_fts"):
                    key = (row["file_name"], str(row["epoch"]))
                    h = _hash(row["content"])
                    have = existing.get(key)
                    if have is not None and have[0] == model and have[1] == h:
                        continue
                    pending.append(
                        {
                            "file_name": row["file_name"],
                            "epoch": str(row["epoch"]),
                            "content": row["content"],
                            "content_hash": h,
                        }
                    )
                    if len(pending) >= limit:
                        break
                return pending
            finally:
                conn.close()

    def store_embeddings(self, rows: list[tuple]) -> int:
        """Persist (file_name, epoch, content_hash, vector) rows. Returns count."""
        import struct

        from core.llm.embeddings import active_model

        model = active_model()
        if not model or not rows:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                now = int(time.time())
                for file_name, epoch, chash, vec in rows:
                    blob = struct.pack(f"<{len(vec)}f", *vec)
                    conn.execute(
                        "INSERT OR REPLACE INTO vectors "
                        "(file_name, epoch, model, dim, content_hash, vec, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (file_name, str(epoch), model, len(vec), chash, blob, now),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO vectors_meta (key, value) VALUES ('model', ?)",
                    (model,),
                )
                conn.commit()
                return len(rows)
            finally:
                conn.close()

    def _prune_orphan_vectors(self, conn) -> int:
        """Drop vector rows whose entry no longer exists in the FTS index.
        Called at the end of reindex(); never re-embeds (snooze work)."""
        try:
            cur = conn.execute(
                "DELETE FROM vectors WHERE NOT EXISTS ("
                "  SELECT 1 FROM memory_fts f"
                "  WHERE f.file_name = vectors.file_name AND CAST(f.epoch AS TEXT) = vectors.epoch"
                ")"
            )
            return cur.rowcount or 0
        except Exception as e:
            logger.warning("Vector prune during reindex failed: %s", e)
            return 0

    def reindex(self) -> int:
        """Rebuild FTS5 index from markdown files. Returns entry count."""
        conn = self._connect()
        try:
            # Clear existing index
            conn.execute("DELETE FROM memory_fts")
            conn.execute("DELETE FROM memory_files")

            total = 0
            for md_path in sorted(self._dir.glob("*.md")):
                if md_path.stat().st_size > 50 * 1024 * 1024:  # 50MB safety cap
                    # Silently dropping a whole file from search is exactly
                    # the kind of thing nobody notices until they go looking
                    # for a memory that used to be there.
                    logger.warning("Skipping oversized memory file during reindex: %s", md_path.name)
                    _notify_oversized_file(md_path)
                    continue
                file_name = md_path.stem
                # A hand-created file whose stem is not a valid memory name
                # ("my notes.md") was indexed anyway — and then every sweep
                # that called read_file(name) on it raised ValueError, which
                # (before the ladder was guarded) ended the whole snooze
                # cycle. Skip it here so the index only ever names files the
                # store can actually open.
                if not _NAME_RE.match(file_name):
                    logger.warning("Skipping memory file with an unusable name: %s", md_path.name)
                    continue
                try:
                    # errors="replace": one stray non-UTF-8 byte from a hand
                    # edit used to raise UnicodeDecodeError here and take the
                    # whole reindex with it.
                    text = md_path.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    logger.warning("Skipping unreadable memory file %s: %s", md_path.name, e)
                    continue
                entries = parse_entries_from_markdown(file_name, text)

                # Register file
                epoch = int(time.time())
                conn.execute(
                    "INSERT OR REPLACE INTO memory_files (name, description, keywords, entry_count, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        file_name,
                        file_name.replace(".", " ").title(),
                        ",".join(file_name.split(".")),
                        len(entries),
                        epoch,
                        epoch,
                    ),
                )

                # Index entries
                for entry in entries:
                    tags = ",".join(entry.tags)
                    conn.execute(
                        "INSERT INTO memory_fts "
                        "(file_name, content, tags, entry_type, weight, epoch, source, updated) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            entry.file_name,
                            entry.content,
                            tags,
                            entry.entry_type,
                            entry.weight,
                            str(entry.epoch),
                            entry.source,
                            str(entry.updated),
                        ),
                    )
                    total += 1

            pruned = self._prune_orphan_vectors(conn)
            conn.commit()
            logger.info(
                "Reindexed %d entries from %d files%s",
                total,
                len(list(self._dir.glob("*.md"))),
                f" (pruned {pruned} orphan vectors)" if pruned else "",
            )
            return total
        finally:
            conn.close()

    def repair_epoch_collisions(self) -> int:
        """Re-epoch duplicate (file, epoch) entries so identity is unique.

        Epochs double as entry identity for update/delete; legacy writes
        could land several entries in the same epoch second within one file,
        making those entries impossible to address individually. The first
        occurrence keeps its epoch; later duplicates are bumped to the next
        free value (the same strategy move_entries uses for cross-file
        collisions). Affected files are reindexed. Returns the number of
        entries re-epoched.
        """
        from core.memory.format import is_file_archived

        epoch_re = re.compile(r"<!-- @epoch:\s*(\d+)\s*-->")
        repaired = 0

        for md_path in sorted(self._dir.glob("*.md")):
            file_name = md_path.stem
            with self._lock:
                raw = md_path.read_text(encoding="utf-8")
                if is_file_archived(raw):
                    continue

                all_epochs = {int(m) for m in epoch_re.findall(raw)}
                sections = raw.split("\n---\n")
                seen: set[int] = set()
                bodies: dict[int, str] = {}
                new_sections = []
                changed = 0

                for section in sections:
                    m = epoch_re.search(section)
                    if not m:
                        new_sections.append(section)
                        continue
                    epoch = int(m.group(1))
                    if epoch in seen and _entry_body(section) == bodies.get(epoch):
                        # An identical twin — the same entry merged into this
                        # file twice by two consolidation passes (only the
                        # @merged_from/@merged_at bookkeeping differs). Two
                        # distinct epochs would turn one duplicate into two
                        # real entries; drop the copy instead. delete_entry
                        # refuses such twins ("legacy collision") and the
                        # exact-duplicate sweep cannot see them (one index
                        # row per epoch), so this is the only place they die.
                        changed += 1
                        continue
                    if epoch in seen:
                        new_epoch = epoch
                        while new_epoch in seen or new_epoch in all_epochs:
                            new_epoch += 1
                        section = section.replace(m.group(0), f"<!-- @epoch: {new_epoch} -->", 1)
                        all_epochs.add(new_epoch)
                        epoch = new_epoch
                        changed += 1
                    seen.add(epoch)
                    bodies[epoch] = _entry_body(section)
                    new_sections.append(section)

                if not changed:
                    continue

                new_raw = "\n---\n".join(new_sections)
                self._write_locked(md_path, new_raw, on_written=lambda: self._reindex_commit(file_name, new_raw))

                repaired += changed
                logger.info("Repaired %d epoch collision(s) in '%s'", changed, file_name)

        return repaired

    def health_check(self, fix: bool = False) -> dict:
        """Check index health. Optionally auto-fix by reindexing and
        repairing epoch collisions."""
        conn = self._connect()
        try:
            # Count indexed entries
            row = conn.execute("SELECT COUNT(*) as cnt FROM memory_fts").fetchone()
            indexed = row["cnt"] if row else 0

            # Count markdown entries and per-file duplicate epochs
            md_count = 0
            collisions = 0
            for md_path in self._dir.glob("*.md"):
                if not _NAME_RE.match(md_path.stem):
                    continue  # not indexable (see reindex) — not a drift signal either
                try:
                    raw = md_path.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    logger.warning("Skipping unreadable memory file %s: %s", md_path.name, e)
                    continue
                entries = parse_entries_from_markdown(md_path.stem, raw)
                md_count += len(entries)
                epochs = [e.epoch for e in entries]
                collisions += len(epochs) - len(set(epochs))

            in_sync = indexed == md_count
            result = {
                "indexed_entries": indexed,
                "markdown_entries": md_count,
                "in_sync": in_sync,
                "epoch_collisions": collisions,
                "files": len(list(self._dir.glob("*.md"))),
            }

            if collisions and fix:
                result["repaired_epoch_collisions"] = self.repair_epoch_collisions()
                result["epoch_collisions"] = 0

            if not in_sync and fix:
                self.reindex()
                result["action"] = "reindexed"
                result["in_sync"] = True

            return result
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_store: MemoryStore | None = None
_store_lock = threading.Lock()
_startup_health_checked = False


def get_memory_store() -> MemoryStore | None:
    """Thread-safe lazy singleton. Runs a health check on first init to catch
    index drift caused by external file edits or backup restorations."""
    global _store, _startup_health_checked
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        try:
            _store = MemoryStore()
            logger.info("Memory store initialized at %s", settings.memory_dir)
        except Exception as e:
            logger.warning("Failed to init memory store: %s. Memory features disabled.", e)
            return None
        if not _startup_health_checked:
            _startup_health_checked = True
            try:
                result = _store.health_check(fix=True)
                if result.get("action") == "reindexed":
                    logger.info(
                        "Memory index was stale on startup; reindexed %d entries across %d file(s)",
                        result.get("indexed_entries", 0),
                        result.get("files", 0),
                    )
            except Exception as hc_err:
                logger.warning("Startup memory health check failed (non-fatal): %s", hc_err)
    return _store
