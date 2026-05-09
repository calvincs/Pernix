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
from core.memory.search import SearchResult, search_bm25, search_hybrid, search_recent
from db.database import connect_memory

logger = logging.getLogger("pernix.memory")

# Namespace auto-routing keywords
NAMESPACE_KEYWORDS = {
    "user.profile": ["user", "profile", "age", "location", "name", "preference", "likes", "dislikes"],
    "pernix.decisions": ["decided", "decision", "chose", "rationale", "why we"],
    "pernix.preferences": ["prefer", "preference", "style", "convention", "always", "never"],
    "pernix.research": ["found", "research", "discovered", "learned", "source"],
    "pernix.debugging": ["debug", "fix", "bug", "error", "workaround", "solved"],
    "pernix.config": ["config", "setting", "environment", "variable", "parameter"],
    "pernix.tools": ["tool", "function", "utility", "command", "usage pattern"],
    "pernix.tasks": ["task", "todo", "milestone", "goal", "objective"],
    "pernix.notes": [],  # default fallback
}


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
    ) -> str:
        """Append an entry to a memory file and index it.

        If file_name is None, auto-routes to best matching file.
        Returns confirmation string.
        """
        if not content.strip():
            return "Error: Empty content"

        # Only dedup substantive entries — short strings (< 60 chars) have
        # unreliable similarity scores and are allowed through unconditionally.
        if len(content) >= 60 and self.is_duplicate(content):
            return "Memory already contains similar content — entry skipped (duplicate)"

        epoch = epoch or int(time.time())

        # Resolve file name: map to existing file or create new one
        file_name = self._resolve_file_name(file_name, content)

        file_name = self._validate_name(file_name)

        # Ensure file exists
        self._ensure_file(file_name, content)

        # Format and append to markdown (with file lock)
        md_path = self._dir / f"{file_name}.md"
        formatted = format_entry(content, entry_type, tags, weight, source=source, epoch=epoch)

        with self._lock:
            # File lock + DB commit must be atomic to prevent index/markdown drift.
            # Keep fcntl lock held until DB commit completes.
            with open(md_path, "a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(formatted)
                    f.flush()

                    # Index in FTS5 while file lock is held
                    tag_list = tags if tags else ",".join(self._infer_tags(content, file_name))
                    conn = self._connect()
                    try:
                        conn.execute(
                            "INSERT INTO memory_fts (file_name, content, tags, entry_type, weight, epoch) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (file_name, content, tag_list, entry_type, weight, str(epoch)),
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

        return f"Saved to {file_name} (epoch={epoch})"

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Canonicalize a file name for comparison.

        Strips extensions, noise suffixes, normalizes separators to underscore.
        """
        name = name.lower()
        # Strip common format-like suffixes (files ending in _txt, _json, etc.)
        for suffix in ("_txt", "_json", "_py", "_html", "_log", "_csv"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        # Normalize separators to underscore
        name = re.sub(r"[-.]", "_", name)
        # Collapse double underscores
        while "__" in name:
            name = name.replace("__", "_")
        # Strip noise suffixes that don't add topical value
        for noise in (
            "_notes",
            "_log",
            "_summary",
            "_overview",
            "_report",
            "_analysis",
            "_strategy",
            "_guide",
            "_spec",
            "_template",
        ):
            if name.endswith(noise):
                name = name[: -len(noise)]
        return name.strip("_")

    @staticmethod
    def _name_tokens(name: str) -> set[str]:
        """Split a file name into word tokens."""
        return {t for t in re.split(r"[._-]", name.lower()) if len(t) > 2}

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

    def is_duplicate(self, content: str, threshold: float = 0.70) -> bool:
        """Multi-signal dedup check. Returns True if content is a duplicate.

        Checks top-3 BM25 results with both SequenceMatcher and bag-of-words
        Jaccard similarity. Catches semantic duplicates that single-result
        SequenceMatcher misses.
        """
        candidates = self.search(content, limit=3, _track_hits=False)
        if not candidates:
            return False

        content_words = set(content.lower().split())

        for r in candidates:
            # Signal 1: SequenceMatcher
            sim = SequenceMatcher(None, content, r.entry.content).ratio()
            if sim > threshold:
                return True

            # Signal 2: bag-of-words Jaccard
            existing_words = set(r.entry.content.lower().split())
            if len(content_words) > 3 and len(existing_words) > 3:
                jaccard = len(content_words & existing_words) / len(content_words | existing_words)
                if jaccard > 0.55:
                    return True

        return False

    # ------------------------------------------------------------------
    # Entry-level mutations (update / delete)
    # ------------------------------------------------------------------

    def _reindex_file(self, conn, file_name: str, new_raw: str, delta_count: int = 0) -> None:
        """Rebuild FTS5 index for a file from updated raw markdown content.

        Deletes all existing rows for the file and re-inserts from parsed entries.
        Uses a file-level delete because FTS5 compound WHERE on UNINDEXED columns
        is unreliable — only equality on FTS-indexed columns or rowid is safe.
        delta_count is added to the stored entry_count (use -1 for a deleted entry).
        """
        from core.memory.format import parse_entries_from_markdown

        conn.execute("DELETE FROM memory_fts WHERE file_name = ?", (file_name,))
        entries = parse_entries_from_markdown(file_name, new_raw)
        for e in entries:
            tag_str = ",".join(e.tags) if e.tags else ""
            conn.execute(
                "INSERT INTO memory_fts (file_name, content, tags, entry_type, weight, epoch) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (file_name, e.content, tag_str, e.entry_type, e.weight, str(e.epoch)),
            )
        now = int(time.time())
        conn.execute(
            "UPDATE memory_files SET entry_count = MAX(0, entry_count + ?), updated_at = ? WHERE name = ?",
            (delta_count, now, file_name),
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

        epoch_marker = f"<!-- @epoch: {epoch} -->"

        with self._lock:
            raw = md_path.read_text(encoding="utf-8")
            sections = raw.split("\n---\n")

            found = False
            new_sections = []
            for section in sections:
                if epoch_marker in section:
                    found = True
                    # Preserve all HTML comment metadata lines exactly as-is,
                    # replace only the content (non-comment) lines.
                    meta_lines = [ln for ln in section.split("\n") if ln.strip().startswith("<!--")]
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
                        self._reindex_file(conn, file_name, new_raw, delta_count=0)
                        conn.commit()
                    finally:
                        conn.close()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        logger.info("Updated memory entry epoch=%d in '%s'", epoch, file_name)
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
                        self._reindex_file(conn, file_name, new_raw, delta_count=-1)
                        conn.commit()
                    finally:
                        conn.close()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        logger.info("Deleted memory entry epoch=%d from '%s'", epoch, file_name)
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
                    "INSERT INTO memory_fts (file_name, content, tags, entry_type, weight, epoch) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (target_file, entry.content, tag_list, entry.entry_type, entry.weight, str(actual_epoch)),
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

    def search_lessons(self, query: str, limit: int = 5) -> list[SearchResult]:
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
        if out:
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

    def recall_enhanced(self, user_message: str, min_score: float = 2.0) -> str:
        """Multi-signal recall for system prompt / scout injection.

        Signal 1: BM25 keyword search (top 5)
        Signal 2: Today's entries (top 3)
        Dedup, filter by min_score, budget to 2500 chars.
        """
        results: list[SearchResult] = []

        conn = self._connect()
        try:
            # Signal 1: keyword search
            results.extend(search_bm25(conn, user_message, limit=5))
            # Signal 2: today's entries
            midnight = int(time.time()) - (int(time.time()) % 86400)
            results.extend(search_recent(conn, limit=3, hours=24))
        finally:
            conn.close()

        # Dedup by (file_name, epoch)
        seen: dict[tuple, SearchResult] = {}
        for r in results:
            key = (r.entry.file_name, r.entry.epoch)
            if key not in seen or r.score > seen[key].score:
                seen[key] = r

        # Filter and sort
        filtered = [r for r in seen.values() if r.score >= min_score]
        filtered.sort(key=lambda r: r.score, reverse=True)

        # Budget to 2500 chars
        lines = []
        total = 0
        for r in filtered:
            line = f"[{r.entry.file_name} score={r.score:.1f}] {r.entry.content[:400]}"
            if total + len(line) > 2500:
                break
            lines.append(line)
            total += len(line)

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
                        "INSERT INTO memory_fts (file_name, content, tags, entry_type, weight, epoch) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (entry.file_name, entry.content, tags, entry.entry_type, entry.weight, str(entry.epoch)),
                    )
                    total += 1

            conn.commit()
            logger.info("Reindexed %d entries from %d files", total, len(list(self._dir.glob("*.md"))))
            return total
        finally:
            conn.close()

    def health_check(self, fix: bool = False) -> dict:
        """Check index health. Optionally auto-fix by reindexing."""
        conn = self._connect()
        try:
            # Count indexed entries
            row = conn.execute("SELECT COUNT(*) as cnt FROM memory_fts").fetchone()
            indexed = row["cnt"] if row else 0

            # Count markdown entries
            md_count = 0
            for md_path in self._dir.glob("*.md"):
                entries = parse_entries_from_markdown(md_path.stem, md_path.read_text())
                md_count += len(entries)

            in_sync = indexed == md_count
            result = {
                "indexed_entries": indexed,
                "markdown_entries": md_count,
                "in_sync": in_sync,
                "files": len(list(self._dir.glob("*.md"))),
            }

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
