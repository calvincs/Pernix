"""Pernix — Memory search: BM25 via SQLite FTS5 + temporal signals."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from core.memory.format import MemoryEntry

logger = logging.getLogger("pernix.memory.search")


@dataclass
class SearchResult:
    """A search result with scoring metadata."""

    entry: MemoryEntry
    score: float
    source: str  # "bm25" | "temporal" | "ripgrep"


def prepare_fts_query(query: str) -> str:
    """Convert natural language query to FTS5 query syntax.

    Strips all punctuation except hyphens, leaving the FTS5 unicode61
    tokenizer to do the rest. Hyphens are preserved so compound terms like
    huggingface-cache flow through; internal dots/colons/slashes/tildes are
    stripped because FTS5 treats word:foo as a column filter (yielding
    "no such column: word") and ~ ? / . as syntax operators.
    """
    # Strip everything that isn't a word char, whitespace, or hyphen.
    # This avoids FTS5 column-filter (foo:bar) and operator (~ ? / .) parsing.
    clean = re.sub(r"[^\w\s-]", " ", query)
    # Strip leading/trailing hyphens per token; keep ≥ 2-char tokens
    # (includes day/month numbers like "04", "27" in date queries)
    words = [w.strip("-") for w in clean.split()]
    words = [w for w in words if len(w) >= 2]
    if not words:
        return f'"{query}"'
    # Quote every token: neutralizes FTS5 operators (NOT/AND/OR/NEAR), reserved
    # keywords, and bare hyphens (e.g. "foo-bar" parses as column-filter syntax).
    # The unicode61 tokenizer still splits inner words for matching.
    return " OR ".join(f'"{w}"' for w in words)


def _ripgrep_fallback(query: str, memory_dir: str, limit: int) -> list[SearchResult]:
    """Last-resort search via ripgrep when FTS5 returns no results.

    Only fires when the full hybrid search (BM25 + temporal) returns nothing,
    so it never affects normal-path performance. Returns rough MemoryEntry
    objects built from matched lines — no epoch/tag metadata.
    """
    rg = shutil.which("rg") or "/usr/bin/rg"
    if not Path(rg).exists():
        return []

    # Use the longest token as the primary rg pattern (most specific)
    tokens = [w.strip("-@:.") for w in re.sub(r"[^\w\s-]", " ", query).split()]
    tokens = [t for t in tokens if len(t) >= 3]
    if not tokens:
        return []
    pattern = max(tokens, key=len)

    try:
        proc = subprocess.run(
            [rg, "--ignore-case", "--json", "--max-count", "3", pattern, memory_dir],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug("ripgrep fallback failed: %s", e)
        return []

    results: list[SearchResult] = []
    seen: set[tuple[str, str]] = set()

    for line in proc.stdout.splitlines():
        try:
            data = json.loads(line)
            if data.get("type") != "match":
                continue
            file_path = data["data"]["path"]["text"]
            file_name = Path(file_path).stem
            if file_name == "_index":
                continue
            match_text = data["data"]["lines"]["text"].strip()
            key = (file_name, match_text[:80])
            if key in seen:
                continue
            seen.add(key)
            entry = MemoryEntry(
                file_name=file_name,
                content=match_text,
                epoch=0,
                entry_type="note",
                tags=[],
                weight="normal",
            )
            results.append(SearchResult(entry=entry, score=1.0, source="ripgrep"))
            if len(results) >= limit:
                break
        except (json.JSONDecodeError, KeyError):
            continue

    return results


def rg_memory_text(pattern: str, memory_dir: str, max_matches: int = 10) -> str:
    """Ripgrep search returning formatted text for LLM consumption (deep_recall tool).

    Returns a human-readable summary grouped by file, suitable for the
    deep_recall agent to synthesize from.
    """
    rg = shutil.which("rg") or "/usr/bin/rg"
    if not Path(rg).exists():
        return "ripgrep not available on this system."

    try:
        proc = subprocess.run(
            [rg, "--ignore-case", "--json", "--max-count", "5", pattern, memory_dir],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return f"rg error: {e}"

    if not proc.stdout.strip():
        return "No matches found."

    files_matches: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        try:
            data = json.loads(line)
            if data.get("type") != "match":
                continue
            file_name = Path(data["data"]["path"]["text"]).stem
            if file_name == "_index":
                continue
            line_num = data["data"]["line_number"]
            text = data["data"]["lines"]["text"].strip()
            files_matches.setdefault(file_name, []).append(f"  line {line_num}: {text}")
        except (json.JSONDecodeError, KeyError):
            continue

    if not files_matches:
        return "No matches found."

    output_lines = []
    for fname, matches in list(files_matches.items())[:max_matches]:
        output_lines.append(f"[{fname}]")
        output_lines.extend(matches[:5])
    return "\n".join(output_lines)


def search_bm25(conn, query: str, limit: int = 5, after_epoch: int | None = None) -> list[SearchResult]:
    """BM25 keyword search via FTS5."""
    fts_query = prepare_fts_query(query)
    if not fts_query.strip():
        return []

    sql = """
        SELECT file_name, content, tags, entry_type, weight, epoch,
               bm25(memory_fts, 1.0, 2.0, 1.5, 0.5, 0.0) as score
        FROM memory_fts
        WHERE memory_fts MATCH ?
    """
    params: list = [fts_query]

    if after_epoch:
        sql += " AND CAST(epoch AS INTEGER) > ?"
        params.append(after_epoch)

    sql += " ORDER BY score LIMIT ?"
    params.append(limit * 2)  # over-fetch for dedup headroom

    results = []
    try:
        for row in conn.execute(sql, params):
            entry = MemoryEntry(
                file_name=row["file_name"],
                content=row["content"],
                epoch=int(row["epoch"]),
                entry_type=row["entry_type"],
                tags=[t.strip() for t in (row["tags"] or "").split(",") if t.strip()],
                weight=row["weight"],
            )
            # BM25 returns negative scores (more negative = more relevant)
            raw_score = abs(row["score"])
            if entry.weight == "high":
                raw_score *= 1.5
            results.append(SearchResult(entry=entry, score=raw_score, source="bm25"))
    except Exception as e:
        logger.warning("BM25 search failed: %s", e)

    return results


def search_recent(conn, limit: int = 3, hours: int = 24) -> list[SearchResult]:
    """Temporal search: recent entries regardless of keyword match."""
    cutoff = int(time.time()) - (hours * 3600)

    results = []
    try:
        rows = conn.execute(
            """SELECT file_name, content, tags, entry_type, weight, epoch
               FROM memory_fts
               WHERE CAST(epoch AS INTEGER) > ?
               ORDER BY CAST(epoch AS INTEGER) DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()

        for row in rows:
            entry = MemoryEntry(
                file_name=row["file_name"],
                content=row["content"],
                epoch=int(row["epoch"]),
                entry_type=row["entry_type"],
                tags=[t.strip() for t in (row["tags"] or "").split(",") if t.strip()],
                weight=row["weight"],
            )
            results.append(SearchResult(entry=entry, score=1.0, source="temporal"))
    except Exception as e:
        logger.warning("Temporal search failed: %s", e)

    return results


def search_hybrid(conn, query: str, limit: int = 5, after_epoch: int | None = None) -> list[SearchResult]:
    """Multi-signal search: BM25 + temporal + ripgrep fallback. Deduped by (file, epoch)."""
    results = []

    # Signal 1: BM25 keyword search
    results.extend(search_bm25(conn, query, limit=limit, after_epoch=after_epoch))

    # Signal 2: Recent entries (last 24h)
    results.extend(search_recent(conn, limit=min(limit, 3)))

    # Deduplicate by (file_name, epoch), keeping highest score
    seen: dict[tuple, SearchResult] = {}
    for r in results:
        key = (r.entry.file_name, r.entry.epoch)
        if key not in seen or r.score > seen[key].score:
            seen[key] = r

    # Sort by score descending, return top N
    final = sorted(seen.values(), key=lambda r: r.score, reverse=True)[:limit]

    # Signal 3: ripgrep fallback — only when FTS5 + temporal both returned nothing.
    # Handles stale/out-of-sync index without waiting for snooze reconciliation.
    if not final:
        from config import settings as _s

        fallback = _ripgrep_fallback(query, _s.memory_dir, limit)
        if fallback:
            logger.debug("Hybrid search empty; ripgrep fallback returned %d result(s)", len(fallback))
        final = fallback

    return final
