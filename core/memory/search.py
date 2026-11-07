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
    source: str  # "bm25" | "vector" | "temporal" | "link"


def format_result_line(result: SearchResult, char_cap: int = 0) -> str:
    """Standard model-facing line for a memory search result.

    Shows the entry date (the correction date when the entry has been
    updated) and write provenance (@source: user / distill / ingest /
    snooze / consolidate) so the model can judge staleness and trust —
    e.g. weigh auto-distilled web content differently from a fact the
    user asked to remember — without extra lookups.
    """
    e = result.entry
    parts = [e.file_name, f"epoch={e.epoch}"]
    # getattr-tolerant: test-shim entries may lack fields.
    updated = int(getattr(e, "updated", 0) or 0)
    ts = updated or int(e.epoch or 0)
    if ts:
        label = "updated" if updated else "date"
        parts.append(f"{label}={time.strftime('%Y-%m-%d', time.localtime(ts))}")
    parts.append(f"score={result.score:.1f}")
    parts.append(f"type={e.entry_type}")
    src = getattr(e, "source", "")
    if src:
        parts.append(f"source={src}")
    # unverified-distill was only ever a tag, and tags never reached this
    # header — so a low-grounding distill claim rendered indistinguishably
    # from a verified fact (ARC-2 field case: a worker treated a "Solved ..."
    # distill entry as authoritative). Say it in the line the model reads.
    if "unverified-distill" in (getattr(e, "tags", None) or []):
        parts.append("UNVERIFIED-DISTILL")
    content = e.content or ""
    if char_cap and len(content) > char_cap:
        content = content[:char_cap]
    return f"[{' '.join(parts)}] {content}"


def prepare_fts_query(query: str) -> tuple[str, int]:
    """Convert natural language query to FTS5 query syntax.

    Strips all punctuation except hyphens, leaving the FTS5 unicode61
    tokenizer to do the rest. Hyphens are preserved so compound terms like
    huggingface-cache flow through; internal dots/colons/slashes/tildes are
    stripped because FTS5 treats word:foo as a column filter (yielding
    "no such column: word") and ~ ? / . as syntax operators.

    Returns (fts_query, token_count); token_count is used to length-
    normalize BM25 scores.
    """
    # The one piece of syntax we honor before stripping: the advertised
    # "@tags: foo" filter (RULES.md, the recall tool, scout's prompt). Each
    # captured tag becomes a real FTS5 column filter ANDed onto the query.
    tag_filters = [t for m in re.finditer(r"@tags:\s*([\w,-]+)", query) for t in m.group(1).split(",") if t]
    query = re.sub(r"@tags:\s*[\w,-]+", " ", query)

    # Strip everything that isn't a word char, whitespace, or hyphen.
    # This avoids FTS5 column-filter (foo:bar) and operator (~ ? / .) parsing.
    clean = re.sub(r"[^\w\s-]", " ", query)
    # Strip leading/trailing hyphens per token; keep ≥ 2-char tokens
    # (includes day/month numbers like "04", "27" in date queries)
    words = [w.strip("-") for w in clean.split()]
    words = [w for w in words if len(w) >= 2]
    if not words and not tag_filters:
        return f'"{query}"', 1

    tags_clause = " AND ".join(f'tags:"{t.strip("-")}"' for t in tag_filters)
    if not words:
        return tags_clause, max(1, len(tag_filters))
    # Quote every token: neutralizes FTS5 operators (NOT/AND/OR/NEAR), reserved
    # keywords, and bare hyphens (e.g. "foo-bar" parses as column-filter syntax).
    # The unicode61 tokenizer still splits inner words for matching.
    or_clause = " OR ".join(f'"{w}"' for w in words)
    if tags_clause:
        return f"({or_clause}) AND {tags_clause}", len(words)
    return or_clause, len(words)


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


def search_bm25(
    conn,
    query: str,
    limit: int = 5,
    after_epoch: int | None = None,
    file_prefix: str | None = None,
    entry_type: str | None = None,
) -> list[SearchResult]:
    """BM25 keyword search via FTS5. Scores are length-normalized.

    entry_type restricts the match to one type in SQL (lessons: 1,683 of
    13.7k entries — post-filtering a top-k over everything starved them).

    Query tokens are OR'd, so the raw bm25() sum grows with query length:
    on the live store a 15-token query with no genuinely relevant entries
    summed to ~12 while a relevant 2-token query reached ~7 — raw values
    are incomparable and absolute thresholds meaningless. Dividing by the
    token count makes the documented scale (> 3.0 strong · 1.0–3.0 weak ·
    < 1.0 noise) hold across query lengths. Known limit: a short query
    where one rare token matches still scores high — lexical search can't
    see intent.
    """
    fts_query, n_tokens = prepare_fts_query(query)
    if not fts_query.strip():
        return []

    sql = """
        SELECT file_name, content, tags, entry_type, weight, epoch, source, updated,
               bm25(memory_fts, 1.0, 2.0, 1.5, 0.5, 0.0) as score
        FROM memory_fts
        WHERE memory_fts MATCH ?
    """
    params: list = [fts_query]

    if after_epoch:
        sql += " AND CAST(epoch AS INTEGER) > ?"
        params.append(after_epoch)

    if entry_type:
        sql += " AND entry_type = ?"
        params.append(entry_type)

    if file_prefix:
        # Space-scoped supplemental query (v33): restrict to one file-name
        # prefix. LIKE with an escaped literal — prefixes come from slugs
        # ([a-z0-9_-]) plus dots, so no wildcard chars, but escape anyway.
        sql += " AND file_name LIKE ? ESCAPE '\\'"
        params.append(file_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")

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
                source=row["source"] or "",
                updated=int(row["updated"] or 0),
            )
            # BM25 returns negative scores (more negative = more relevant);
            # normalize by query length so scores compare across queries.
            score = abs(row["score"]) / max(1, n_tokens)
            if entry.weight == "high":
                score *= 1.5
            results.append(SearchResult(entry=entry, score=score, source="bm25"))
    except Exception as e:
        logger.warning("BM25 search failed: %s", e)

    return results


def search_recent(conn, limit: int = 3, hours: int = 24) -> list[SearchResult]:
    """Temporal search: recent entries regardless of keyword match."""
    cutoff = int(time.time()) - (hours * 3600)

    results = []
    try:
        rows = conn.execute(
            """SELECT file_name, content, tags, entry_type, weight, epoch, source, updated
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
                source=row["source"] or "",
                updated=int(row["updated"] or 0),
            )
            results.append(SearchResult(entry=entry, score=1.0, source="temporal"))
    except Exception as e:
        logger.warning("Temporal search failed: %s", e)

    return results


def search_vector(conn, query_vec: list[float], limit: int = 5) -> list[SearchResult]:
    """Cosine similarity over the vectors sidecar (adaptation plan 1f).

    Brute force over an in-memory float32 matrix — corpus sizes here are
    thousands, not millions; no ANN dependency. Only rows embedded by the
    CURRENT model participate, so a model change can never mix spaces.
    """
    from core.llm.embeddings import active_model

    try:
        import numpy as np
    except ImportError:
        return []

    try:
        rows = conn.execute(
            "SELECT file_name, epoch, dim, vec FROM vectors WHERE model = ?",
            (active_model(),),
        ).fetchall()
    except Exception as e:
        logger.debug("Vector search unavailable: %s", e)
        return []
    if not rows:
        return []

    dim = rows[0]["dim"]
    rows = [r for r in rows if r["dim"] == dim]
    q = np.asarray(query_vec, dtype=np.float32)
    if q.shape[0] != dim:
        return []  # stale query-model mismatch; lexical carries the search
    mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32, count=dim) for r in rows])
    norms = np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) or 1.0)
    norms[norms == 0] = 1.0
    scores = (mat @ q) / norms

    top = np.argsort(scores)[::-1][:limit]
    results: list[SearchResult] = []
    for idx in top:
        r = rows[int(idx)]
        row = conn.execute(
            "SELECT file_name, content, tags, entry_type, weight, epoch, source, updated "
            "FROM memory_fts WHERE file_name = ? AND CAST(epoch AS TEXT) = ?",
            (r["file_name"], str(r["epoch"])),
        ).fetchone()
        if row is None:
            continue  # orphan vector; reindex prune will collect it
        entry = MemoryEntry(
            file_name=row["file_name"],
            content=row["content"],
            epoch=int(row["epoch"]),
            entry_type=row["entry_type"],
            tags=[t.strip() for t in (row["tags"] or "").split(",") if t.strip()],
            weight=row["weight"],
            source=row["source"] or "",
            updated=int(row["updated"] or 0),
        )
        results.append(SearchResult(entry=entry, score=float(scores[int(idx)]), source="vector"))
    return results


_RRF_K = 60  # standard reciprocal-rank-fusion constant

# Term coverage for the fused lexical channel (2026-11-06). Query tokens are
# OR'd and the bm25 sum is divided by token count, so an entry matching ONE
# token of a two-word query scores like a full match when that token is
# frequent in it: "sagging middle" put a "Middle East" policy entry at 6.6,
# "strong" on the shared scale. Scaling by the squared share of query terms
# the entry contains keeps a full match untouched (1.0), halves-squared a
# one-of-two match (0.25), and reorders the lexical list toward entries that
# answer more of the question. Matching is a prefix test that tolerates the
# porter stemmer's suffix folding; it only has to agree with FTS5 roughly.
_COVERAGE_POWER = 2


def query_terms(query: str) -> list[str]:
    """The distinct lowercased words prepare_fts_query matches on."""
    clean = re.sub(r"@tags:\s*[\w,-]+", " ", query)
    clean = re.sub(r"[^\w\s-]", " ", clean)
    words: list[str] = []
    for w in re.split(r"[\s-]+", clean.lower()):
        w = w.strip("-_")
        if len(w) >= 2 and w not in words:
            words.append(w)
    return words


def _stem_key(word: str) -> str:
    return word[: max(4, len(word) - 3)] if len(word) > 4 else word


def term_coverage(entry: MemoryEntry, terms: list[str]) -> float:
    """Share of `terms` that appear in the entry (name, content or tags)."""
    if len(terms) < 2:
        return 1.0
    text = f"{entry.file_name} {entry.content} {' '.join(entry.tags or [])}".lower()
    words = set(re.split(r"[^\w]+", text))
    hit = 0
    for t in terms:
        key = _stem_key(t)
        if t in words or any(w.startswith(key) for w in words if len(w) >= len(key)):
            hit += 1
    return hit / len(terms)


def _with_term_coverage(r: SearchResult, terms: list[str]) -> SearchResult:
    cov = term_coverage(r.entry, terms)
    if cov >= 1.0:
        return r
    return SearchResult(entry=r.entry, score=r.score * cov**_COVERAGE_POWER, source=r.source)


# Cosine → BM25-scale mapping for entries only the semantic channel surfaced.
# Every consumer of `score` reads one absolute scale (RULES.md, recall/
# search_lessons, internal_recall's strong-match nudge): > 3.0 strong ·
# 1.0–3.0 weak · < 1.0 noise. Raw RRF scores cap at ~2/(60+1) ≈ 0.033, which
# reads as noise everywhere — so fusion decides *order* only and the exposed
# score stays on the BM25 scale.
#
# The map is affine between two measured anchors (2026-11-06, live box,
# nomic-embed-text-v2-moe with task prefixes, 40 paraphrased queries with a
# known answer): the right entry's cosine had p25/p50/p75 = 0.48/0.53/0.59
# and an off-topic query's best hit 0.26–0.38. 0.50 maps to 3.0 (strong),
# 0.30 to 1.0 (noise floor). The old linear cosine × 4.0 put the median
# right answer at 2.1 — "weak" — so the model was told to distrust exactly
# the hits the semantic channel exists for. A model or embed-format change
# moves both anchors: re-measure them.
_VECTOR_NOISE_COS = 0.30
_VECTOR_STRONG_COS = 0.50
_VECTOR_SCORE_MAX = 6.0


def vector_to_scale(cos: float) -> float:
    """Map a cosine onto the shared BM25 score scale (see anchors above)."""
    span = _VECTOR_STRONG_COS - _VECTOR_NOISE_COS
    score = 1.0 + (cos - _VECTOR_NOISE_COS) * (2.0 / span)
    return max(0.0, min(_VECTOR_SCORE_MAX, score))


def _rrf_fuse(lexical: list[SearchResult], semantic: list[SearchResult], limit: int) -> list[SearchResult]:
    """Fuse the lexical and semantic channels, deduped by (file, epoch).

    Ordering is reciprocal-rank fusion over both channels. The reported
    `score` is NOT the RRF sum (see the anchors above): a result BM25 saw
    keeps its BM25 score untouched, a vector-only result gets its cosine
    mapped onto the same scale, and a result both saw takes the higher of
    the two readings. `source` names the channel that contributed
    the most rank weight, so a result that owes its position to the vector
    channel is not mislabeled "bm25".
    """
    fused: dict[tuple, dict] = {}
    for channel, ranked in (("bm25", lexical), ("vector", semantic)):
        for rank, r in enumerate(ranked):
            key = (r.entry.file_name, r.entry.epoch)
            contribution = 1.0 / (_RRF_K + rank + 1)
            slot = fused.get(key)
            if slot is None:
                fused[key] = {
                    "entry": r.entry,
                    "rrf": contribution,
                    "best": contribution,
                    "source": channel,
                    "score": (r.score if channel == "bm25" else vector_to_scale(r.score)),
                }
                continue
            slot["rrf"] += contribution
            # Both channels saw it: the stronger reading wins. Lexical-wins
            # let one shared common word ("middle") label an off-topic entry
            # strong while its cosine said noise; meaning-wins alone would
            # bury exact identifiers the embedder cannot see.
            other = r.score if channel == "bm25" else vector_to_scale(r.score)
            slot["score"] = max(slot["score"], other)
            if contribution > slot["best"]:
                slot["best"] = contribution
                slot["source"] = channel
    ordered = sorted(fused.values(), key=lambda s: s["rrf"], reverse=True)[:limit]
    return [SearchResult(entry=s["entry"], score=s["score"], source=s["source"]) for s in ordered]


# Per-type share of one result set (2026-11-06). Profile (≈2k) and skill
# (≈1.3k) entries outnumber findings and decisions, and their broad wording
# matches almost any query, so they filled whole top-k lists — the lesson
# starvation fixed in search_lessons was one instance of this. No type may
# take more than half the slots while other types have candidates; when
# nothing else matched, the deferred ones backfill, so a profile-only query
# still gets a full list.
_TYPE_CAP_OVERFETCH = 3


def cap_per_type(results: list[SearchResult], limit: int) -> list[SearchResult]:
    """Top `limit` of `results` (already in rank order) with no entry_type
    holding more than ceil(limit/2) slots unless nothing else is left.
    Order within the kept set is preserved."""
    cap = max(1, -(-limit // 2))
    counts: dict[str, int] = {}
    kept: list[SearchResult] = []
    deferred: list[SearchResult] = []
    for r in results:
        t = r.entry.entry_type or ""
        if counts.get(t, 0) < cap:
            counts[t] = counts.get(t, 0) + 1
            kept.append(r)
            if len(kept) >= limit:
                return kept
        else:
            deferred.append(r)
    if len(kept) < limit and deferred:
        rank = {id(r): i for i, r in enumerate(results)}
        kept = sorted(kept + deferred[: limit - len(kept)], key=lambda r: rank[id(r)])
    return kept


def search_hybrid(
    conn,
    query: str,
    limit: int = 5,
    after_epoch: int | None = None,
    file_prefix: str | None = None,
) -> list[SearchResult]:
    """Multi-signal search: BM25 ∪ vector (RRF-fused when embeddings are
    enabled), recent entries pad remaining slots. Deduped by (file, epoch).
    Scores stay on the BM25 scale throughout — see _rrf_fuse.

    file_prefix restricts the lexical channel to one file-name prefix
    (space-scoped supplemental queries, v33); the vector and recent
    channels are post-filtered to match.
    """
    # Each channel over-fetches so the per-type cap below has candidates of
    # other types to promote.
    fetch = limit * _TYPE_CAP_OVERFETCH

    # Signal 1: BM25 keyword search — dedupe keeping highest score
    seen: dict[tuple, SearchResult] = {}
    for r in search_bm25(conn, query, limit=fetch, after_epoch=after_epoch, file_prefix=file_prefix):
        key = (r.entry.file_name, r.entry.epoch)
        if key not in seen or r.score > seen[key].score:
            seen[key] = r
    terms = query_terms(query)
    lexical = [_with_term_coverage(r, terms) for r in seen.values()]
    bm25_ranked = sorted(lexical, key=lambda r: r.score, reverse=True)[:fetch]

    # Signal 1b: semantic channel (1f). Degrades to lexical-only on any
    # failure — unset model, Ollama down, no vectors yet.
    final = bm25_ranked
    from core.llm import embeddings as _emb

    if _emb.enabled():
        query_vec = _emb.embed_query_sync(query)
        if query_vec:
            vector_ranked = search_vector(conn, query_vec, limit=fetch)
            if file_prefix:
                vector_ranked = [r for r in vector_ranked if r.entry.file_name.startswith(file_prefix)]
            if vector_ranked:
                final = _rrf_fuse(bm25_ranked, vector_ranked, fetch)

    final = cap_per_type(final, limit)

    # Signal 2: Recent entries (last 24h) fill remaining slots only — their
    # flat score is relevance-blind, so they must not displace a weak-but-
    # relevant keyword match from top-k.
    if len(final) < limit:
        included = {(r.entry.file_name, r.entry.epoch) for r in final}
        for r in search_recent(conn, limit=min(limit, 3)):
            if len(final) >= limit:
                break
            if file_prefix and not r.entry.file_name.startswith(file_prefix):
                continue
            key = (r.entry.file_name, r.entry.epoch)
            if key not in included:
                included.add(key)
                final.append(r)

    return final


# ---------------------------------------------------------------------------
# Wiki-link expansion (H4, plan §12.5): one hop, read-only, opt-in
# ---------------------------------------------------------------------------


def expand_links(conn, results: list[SearchResult], max_extra: int = 3) -> list[SearchResult]:
    """Pull entries referenced by [[file-name]] / [[file@epoch]] in the
    result contents. One hop, deduped against results already present,
    appended with source="link" so the model can see they arrived by
    reference. An unresolvable ref degrades to no expansion — dangling
    links (e.g. after a re-route renamed a file) are silent by design."""
    from core.memory.format import extract_links

    if not results or max_extra <= 0:
        return results
    have = {(r.entry.file_name, r.entry.epoch) for r in results}
    extra: list[SearchResult] = []
    for r in results:
        for ref in extract_links(r.entry.content):
            if len(extra) >= max_extra:
                break
            file_name, _, epoch_s = ref.partition("@")
            try:
                if epoch_s:
                    rows = conn.execute(
                        "SELECT file_name, content, tags, entry_type, weight, epoch, source, updated "
                        "FROM memory_fts WHERE file_name = ? AND epoch = ?",
                        (file_name, epoch_s),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT file_name, content, tags, entry_type, weight, epoch, source, updated "
                        "FROM memory_fts WHERE file_name = ? "
                        "ORDER BY CAST(epoch AS INTEGER) DESC LIMIT 2",
                        (file_name,),
                    ).fetchall()
            except Exception as e:
                logger.debug("Link expansion query failed for %r: %s", ref, e)
                continue
            for row in rows:
                key = (row["file_name"], int(row["epoch"]))
                if key in have:
                    continue
                have.add(key)
                extra.append(
                    SearchResult(
                        entry=MemoryEntry(
                            file_name=row["file_name"],
                            content=row["content"],
                            epoch=int(row["epoch"]),
                            entry_type=row["entry_type"],
                            tags=[t.strip() for t in (row["tags"] or "").split(",") if t.strip()],
                            weight=row["weight"],
                            source=row["source"] or "",
                            updated=int(row["updated"] or 0),
                        ),
                        score=0.0,
                        source="link",
                    )
                )
    return results + extra
