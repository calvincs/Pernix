"""Pernix — Memory search: BM25 via SQLite FTS5 + temporal signals."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from core.memory.format import MemoryEntry

logger = logging.getLogger("pernix.memory.search")


@dataclass
class SearchResult:
    """A search result with scoring metadata."""

    entry: MemoryEntry
    score: float
    source: str  # "bm25" | "temporal" | "recency"


_FTS5_RESERVED = {"AND", "OR", "NOT", "NEAR"}


def prepare_fts_query(query: str) -> str:
    """Convert natural language query to FTS5 query syntax."""
    # Remove special FTS5 characters
    clean = re.sub(r"[^\w\s]", " ", query)
    words = [w for w in clean.split() if len(w) > 2]
    if not words:
        return f'"{query}"'
    # Quote FTS5 reserved keywords to prevent syntax errors
    safe = [f'"{w}"' if w.upper() in _FTS5_RESERVED else w for w in words]
    return " OR ".join(safe)


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
    """Multi-signal search: BM25 + temporal. Deduped by (file, epoch)."""
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
    final = sorted(seen.values(), key=lambda r: r.score, reverse=True)
    return final[:limit]
