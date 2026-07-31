"""Pernix — Internalized memory store (replaces HyperKB).

Markdown files + SQLite FTS5 index. ~800 lines replacing ~8000.
Markdown files are source of truth; FTS5 index is rebuildable.

Writes are append-by-default (remember, ingest, distill, snooze) with
explicit per-entry mutation via update_entry / delete_entry — used by the
agent's update_memory / forget tools to correct or remove specific entries.
Epochs are immutable: an updated entry keeps its original epoch.
"""

from __future__ import annotations

import fcntl
import logging
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

from config import settings
from core.memory.format import (
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
    ) -> str:
        """Append an entry to a memory file and index it.

        If file_name is None, auto-routes to best matching file.
        Returns confirmation string.

        skip_dedup: bypass the duplicate gate. For writers whose content is
        by construction similar to entries they are about to supersede
        (consolidation fuse) — the gate would block the write against the
        very entry being replaced.
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
                preview = dup.entry.content[:160].replace("\n", " ")
                return (
                    f"Memory already contains similar content — entry skipped (duplicate of "
                    f'{dup.entry.file_name}@{dup.entry.epoch}: "{preview}"). If your version is '
                    f"newer or more accurate, supersede it with "
                    f"update_memory(file='{dup.entry.file_name}', epoch={dup.entry.epoch}, content=...)."
                )

        epoch = epoch or int(time.time())

        # Resolve file name: map to existing file or create new one
        file_name = self._resolve_file_name(file_name, content)

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

    # Canonical implementations live in core.memory.routing (shared with
    # consolidation clustering); kept as static methods for callers/tests.
    _normalize_name = staticmethod(normalize_file_name)
    _name_tokens = staticmethod(name_tokens)

    def _resolve_file_name(self, suggested: str | None, content: str) -> str:
        """Map a suggested file name to an existing file when possible.

        Resolution cascade:
        1. Exact normalized-name match against existing files
        2. Token-Jaccard >= 0.6 against existing files
        3. FTS5 content-based file match (dominant file in top-5 results)
        4. Keyword-based auto-route fallback
        """
        if not suggested:
            return self._auto_route(content)

        # Clean the suggested name through validation-safe form
        try:
            suggested = self._validate_name(suggested)
        except ValueError:
            return self._auto_route(content)

        # Build map of existing files (normalized → actual name)
        conn = self._connect()
        try:
            rows = conn.execute("SELECT name FROM memory_files WHERE entry_count > 0").fetchall()
        finally:
            conn.close()
        existing = {r["name"]: self._normalize_name(r["name"]) for r in rows}

        if not existing:
            return suggested

        suggested_norm = self._normalize_name(suggested)
        suggested_tokens = self._name_tokens(suggested)

        # 1. Exact normalized match
        for actual, norm in existing.items():
            if norm == suggested_norm:
                return actual

        # 2. Token Jaccard >= 0.6
        best_jaccard = 0.0
        best_match = None
        for actual, _ in existing.items():
            actual_tokens = self._name_tokens(actual)
            if not suggested_tokens or not actual_tokens:
                continue
            jaccard = len(suggested_tokens & actual_tokens) / len(suggested_tokens | actual_tokens)
            if jaccard > best_jaccard:
                best_jaccard = jaccard
                best_match = actual
        if best_jaccard >= 0.6 and best_match:
            return best_match

        # 3. FTS5 content-based match
        try:
            conn = self._connect()
            try:
                results = search_bm25(conn, content[:200], limit=5)
            finally:
                conn.close()
            if results:
                file_counts: dict[str, int] = {}
                for r in results:
                    fn = r.entry.file_name
                    file_counts[fn] = file_counts.get(fn, 0) + 1
                dominant = max(file_counts, key=file_counts.get)
                if file_counts[dominant] >= 3:
                    return dominant
        except Exception:
            pass

        # 4. Fallback — use suggested name (or auto-route if it looks generic)
        return suggested

    def _auto_route(self, content: str) -> str:
        """Find best existing file for content, or suggest new one.

        Always evaluates ALL signals (FTS5, namespace keywords, file metadata)
        and combines them. No early returns — prevents gravity-well effect
        where one large file attracts all new entries.
        """
        content_lower = content.lower()
        # Candidates: {file_name: score}
        candidates: dict[str, float] = {}

        # Signal 1: Namespace keyword matching (always runs, cheap)
        for ns, keywords in NAMESPACE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in content_lower)
            if score > 0:
                # Boost namespace matches — these are curated topical buckets
                candidates[ns] = candidates.get(ns, 0) + score * 2.0

        # Signal 2: Existing file metadata keyword overlap
        conn = self._connect()
        try:
            rows = conn.execute("SELECT name, keywords FROM memory_files WHERE entry_count > 0").fetchall()
            file_count = len(rows)
            for row in rows:
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
                    results = search_bm25(conn, content[:200], limit=10)
                finally:
                    conn.close()
                if results:
                    file_scores: dict[str, float] = {}
                    for r in results:
                        fn = r.entry.file_name
                        file_scores[fn] = file_scores.get(fn, 0) + r.score
                    for fn, score in file_scores.items():
                        # Require strong FTS5 signal (>= 3.0) to influence routing
                        if score >= 3.0:
                            candidates[fn] = candidates.get(fn, 0) + score
            except Exception:
                pass

        if candidates:
            return max(candidates, key=candidates.get)
        return "pernix.notes"

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

        Checks top-3 BM25 results with both SequenceMatcher and bag-of-words
        Jaccard similarity. Catches semantic duplicates that single-result
        SequenceMatcher misses.
        """
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

            with open(md_path, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(new_raw)
                    f.flush()
                    conn = self._connect()
                    try:
                        self._reindex_file(conn, file_name, new_raw)
                        conn.commit()
                    finally:
                        conn.close()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

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

            with open(md_path, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(new_raw)
                    f.flush()
                    conn = self._connect()
                    try:
                        self._reindex_file(conn, file_name, new_raw)
                        conn.commit()
                    finally:
                        conn.close()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

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
                with open(md_path, "w") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    f.write(content)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            # Remove all FTS5 entries and zero count
            conn = self._connect()
            try:
                conn.execute("DELETE FROM memory_fts WHERE file_name = ?", (name,))
                conn.execute("UPDATE memory_files SET entry_count = 0 WHERE name = ?", (name,))
                conn.commit()
            finally:
                conn.close()

        logger.info("Archived memory file: %s", name)

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
        limit: int = 5,
        after_epoch: int | None = None,
        _track_hits: bool = True,
    ) -> list[SearchResult]:
        """Search memory entries."""
        conn = self._connect()
        try:
            if mode == "bm25":
                results = search_bm25(conn, query, limit=limit, after_epoch=after_epoch)
            elif mode == "recent":
                results = search_recent(conn, limit=limit)
            else:
                results = search_hybrid(conn, query, limit=limit, after_epoch=after_epoch)
        finally:
            conn.close()

        if _track_hits and results:
            self._record_hits(results)
        return results

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

    def search_lessons(self, query: str, limit: int = 5, _track_hits: bool = True) -> list[SearchResult]:
        """Search lesson-type memory entries with age-based decay.

        Lessons are operational workarounds extracted by snooze_reflect from
        failed sessions. Tagged `lesson` and stored with entry_type='lesson'.

        Age decay: lessons reference a code state at the time they were
        written. The codebase moves on; a lesson that names a "manifest bug"
        from a past run-that-was-since-fixed misleads new scouts into manual
        intervention against a manifest that's now correct. We apply a
        multiplicative decay (1.0 at <14d, 0.5 at <60d, 0.25 at <180d, 0.05
        beyond). Lessons that survive after decay still show up — they're
        just outranked by anything fresher and roughly comparable.
        """
        results = self.search(query, mode="hybrid", limit=limit * 4, _track_hits=False)
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
            lines.append(f"[{r.entry.file_name} score={r.score:.1f}] {r.entry.content[:400]}")
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

    def read_file(self, name: str) -> str | None:
        name = self._validate_name(name)
        md_path = self._dir / f"{name}.md"
        if not md_path.exists():
            return None
        return md_path.read_text()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

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
                    logger.warning("Skipping oversized memory file during reindex: %s", md_path.name)
                    continue
                file_name = md_path.stem
                text = md_path.read_text()
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

            conn.commit()
            logger.info("Reindexed %d entries from %d files", total, len(list(self._dir.glob("*.md"))))
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
                new_sections = []
                changed = 0

                for section in sections:
                    m = epoch_re.search(section)
                    if not m:
                        new_sections.append(section)
                        continue
                    epoch = int(m.group(1))
                    if epoch in seen:
                        new_epoch = epoch
                        while new_epoch in seen or new_epoch in all_epochs:
                            new_epoch += 1
                        section = section.replace(m.group(0), f"<!-- @epoch: {new_epoch} -->", 1)
                        all_epochs.add(new_epoch)
                        epoch = new_epoch
                        changed += 1
                    seen.add(epoch)
                    new_sections.append(section)

                if not changed:
                    continue

                new_raw = "\n---\n".join(new_sections)
                with open(md_path, "w", encoding="utf-8") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        f.write(new_raw)
                        f.flush()
                        conn = self._connect()
                        try:
                            self._reindex_file(conn, file_name, new_raw)
                            conn.commit()
                        finally:
                            conn.close()
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

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
                entries = parse_entries_from_markdown(md_path.stem, md_path.read_text())
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
