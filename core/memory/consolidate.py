"""Pernix — Memory consolidation: cross-file dedup and merge.

Identifies clusters of overlapping memory files and merges them,
preserving all timestamps and hit counts. Runs as a Snooze activity.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from config import settings
from core.memory.format import MemoryEntry

logger = logging.getLogger("pernix.memory.consolidate")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FileSignature:
    """Lightweight summary of a memory file for clustering."""

    name: str
    normalized: str
    name_tokens: set[str]
    keywords: set[str]
    entry_count: int
    oldest_epoch: int = 0
    newest_epoch: int = 0
    content_fingerprints: list[str] = field(default_factory=list)


@dataclass
class MergeDecision:
    """Plan for merging a cluster of files."""

    target_file: str
    source_files: list[str]
    strategy: str  # "trivial" | "llm"
    entries_to_keep: list[tuple[str, int]]  # (file, epoch) to copy to target
    entries_to_archive: list[tuple[str, int]]  # (file, epoch) dupes to archive
    fused_entries: list[dict] | None = None  # LLM-produced fused content
    target_description: str = ""
    target_keywords: list[str] = field(default_factory=list)
    reason: str = ""


# ---------------------------------------------------------------------------
# Filename normalization (shared with store.py)
# ---------------------------------------------------------------------------


def normalize_filename(name: str) -> str:
    """Canonicalize a file name for comparison.

    Strips extensions, noise suffixes, normalizes separators to underscore.
    Matches MemoryStore._normalize_name() logic.
    """
    import re

    name = name.lower()
    for suffix in ("_txt", "_json", "_py", "_html", "_log", "_csv"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    name = re.sub(r"[-.]", "_", name)
    while "__" in name:
        name = name.replace("__", "_")
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


def _name_tokens(name: str) -> set[str]:
    """Split a file name into word tokens (length > 2)."""
    import re

    return {t for t in re.split(r"[._-]", name.lower()) if len(t) > 2}


# ---------------------------------------------------------------------------
# Signature building
# ---------------------------------------------------------------------------


def build_signatures(store) -> list[FileSignature]:
    """Build lightweight signatures for all active memory files."""
    from core.memory.format import parse_entries_from_markdown

    files = store.list_files()
    signatures = []

    for f in files:
        if f.entry_count == 0:
            continue

        md_content = store.read_file(f.name)
        if not md_content:
            continue

        entries = parse_entries_from_markdown(f.name, md_content)
        if not entries:
            continue

        epochs = sorted(e.epoch for e in entries)
        fingerprints = [e.content[:200].lower() for e in entries]

        signatures.append(
            FileSignature(
                name=f.name,
                normalized=normalize_filename(f.name),
                name_tokens=_name_tokens(f.name),
                keywords=set(kw.strip().lower() for kw in f.keywords if kw.strip()),
                entry_count=len(entries),
                oldest_epoch=epochs[0] if epochs else 0,
                newest_epoch=epochs[-1] if epochs else 0,
                content_fingerprints=fingerprints,
            )
        )

    return signatures


# ---------------------------------------------------------------------------
# Pairwise scoring
# ---------------------------------------------------------------------------


def score_pair(a: FileSignature, b: FileSignature) -> float:
    """Compute weighted similarity between two file signatures.

    Weights: normalized-name 0.35, token-Jaccard 0.25,
             keyword-Jaccard 0.15, content-fingerprint 0.25.
    """
    # Normalized name similarity
    if a.normalized == b.normalized:
        name_sim = 1.0
    else:
        name_sim = SequenceMatcher(None, a.normalized, b.normalized).ratio()

    # Token Jaccard
    if a.name_tokens and b.name_tokens:
        token_jaccard = len(a.name_tokens & b.name_tokens) / len(a.name_tokens | b.name_tokens)
    else:
        token_jaccard = 0.0

    # Keyword Jaccard
    if a.keywords and b.keywords:
        kw_jaccard = len(a.keywords & b.keywords) / len(a.keywords | b.keywords)
    else:
        kw_jaccard = 0.0

    # Content fingerprint: best pairwise match
    content_sim = 0.0
    if a.content_fingerprints and b.content_fingerprints:
        best = 0.0
        for fp_a in a.content_fingerprints:
            for fp_b in b.content_fingerprints:
                sim = SequenceMatcher(None, fp_a, fp_b).ratio()
                if sim > best:
                    best = sim
        content_sim = best

    return (0.35 * name_sim) + (0.25 * token_jaccard) + (0.15 * kw_jaccard) + (0.25 * content_sim)


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def find_clusters(
    signatures: list[FileSignature],
    threshold: float | None = None,
) -> list[list[str]]:
    """Single-linkage clustering of files by pairwise similarity.

    Returns clusters of 2+ file names, sorted largest first.
    """
    threshold = threshold or settings.snooze_consolidation_cluster_threshold

    sig_map = {s.name: s for s in signatures}
    names = list(sig_map.keys())

    # Build adjacency graph
    adj: dict[str, set[str]] = {n: set() for n in names}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            sim = score_pair(sig_map[names[i]], sig_map[names[j]])
            if sim >= threshold:
                adj[names[i]].add(names[j])
                adj[names[j]].add(names[i])

    # Extract connected components (BFS)
    visited: set[str] = set()
    clusters: list[list[str]] = []

    for name in names:
        if name in visited:
            continue
        if not adj[name]:
            continue
        # BFS
        component = []
        queue = [name]
        while queue:
            n = queue.pop(0)
            if n in visited:
                continue
            visited.add(n)
            component.append(n)
            for neighbor in adj[n]:
                if neighbor not in visited:
                    queue.append(neighbor)
        if len(component) >= 2:
            clusters.append(component)

    # Sort: largest clusters first
    clusters.sort(key=len, reverse=True)
    return clusters


def prioritize_clusters(
    clusters: list[list[str]],
    sig_map: dict[str, FileSignature],
) -> list[list[str]]:
    """Sort clusters by merge priority.

    Priority: 1) identical normalized names (trivial), 2) cluster size.
    """

    def sort_key(cluster: list[str]) -> tuple[int, int]:
        norms = {sig_map[n].normalized for n in cluster if n in sig_map}
        is_trivial = 1 if len(norms) == 1 else 0
        return (is_trivial, len(cluster))

    return sorted(clusters, key=sort_key, reverse=True)


# ---------------------------------------------------------------------------
# Merge planning
# ---------------------------------------------------------------------------


def plan_trivial_merge(
    cluster: list[str],
    store,
) -> MergeDecision | None:
    """Plan a merge for clusters with the same normalized name.

    Uses SequenceMatcher to identify duplicates, no LLM needed.
    Returns None if the cluster is too ambiguous for trivial merge.
    """
    from core.memory.format import parse_entries_from_markdown

    sig_map = {}
    for name in cluster:
        sig_map[name] = normalize_filename(name)

    # Only trivial if all normalize to the same string
    norms = set(sig_map.values())
    if len(norms) > 1:
        return None

    # Gather all entries across cluster files
    all_entries: list[tuple[str, MemoryEntry]] = []  # (file_name, entry)
    all_keywords: set[str] = set()

    for name in cluster:
        md = store.read_file(name)
        if not md:
            continue
        entries = parse_entries_from_markdown(name, md)
        for e in entries:
            all_entries.append((name, e))
        # Gather keywords from file metadata
        files = store.list_files()
        for f in files:
            if f.name == name:
                all_keywords.update(kw.strip().lower() for kw in f.keywords if kw.strip())

    if not all_entries:
        return None

    # Pick target: file with most entries, tiebreak by oldest epoch
    file_stats: dict[str, tuple[int, int]] = {}
    for name, entry in all_entries:
        count, oldest = file_stats.get(name, (0, entry.epoch))
        file_stats[name] = (count + 1, min(oldest, entry.epoch))

    target = max(file_stats, key=lambda n: (file_stats[n][0], -file_stats[n][1]))

    # Pairwise dedup: for each entry, check if a better version exists
    entries_to_keep: list[tuple[str, int]] = []
    entries_to_archive: list[tuple[str, int]] = []

    for i, (file_i, entry_i) in enumerate(all_entries):
        is_dup = False
        for j, (file_j, entry_j) in enumerate(all_entries):
            if i == j:
                continue
            sim = SequenceMatcher(None, entry_i.content, entry_j.content).ratio()
            if sim > 0.82:
                # Archive the shorter one, or the later one if same length
                if (len(entry_i.content) < len(entry_j.content)) or (
                    len(entry_i.content) == len(entry_j.content) and entry_i.epoch > entry_j.epoch
                ):
                    entries_to_archive.append((file_i, entry_i.epoch))
                    is_dup = True
                    break
        if not is_dup:
            entries_to_keep.append((file_i, entry_i.epoch))

    source_files = [n for n in cluster if n != target]

    return MergeDecision(
        target_file=target,
        source_files=source_files,
        strategy="trivial",
        entries_to_keep=entries_to_keep,
        entries_to_archive=entries_to_archive,
        target_description=target.replace("_", " ").replace("-", " ").replace(".", " ").title(),
        target_keywords=list(all_keywords),
        reason=f"Trivial merge: {len(cluster)} files share normalized name '{norms.pop()}'",
    )


# ---------------------------------------------------------------------------
# LLM merge planning
# ---------------------------------------------------------------------------

_LLM_MERGE_PROMPT = """You are a memory curator. These memory files cover overlapping topics and need consolidation.

Files:
{file_entries}

For each entry, provide a verdict:
- KEEP: unique valuable info, preserve as-is
- ARCHIVE: fully redundant with another entry
- FUSE_WITH: merge with another entry. Produce combined content preserving ALL unique facts from both. Note any date-dependent differences (e.g., "As of YYYY-MM-DD: ...").

Rules:
- NEVER discard unique facts. If in doubt, KEEP.
- For FUSE: keep the OLDEST epoch as timestamp. Include all distinct details from both entries.
- Pick one target file name (prefer the most general/canonical name).
- Output JSON only, no markdown fences.

Output format:
{{"target": "file_name", "entries": [
  {{"file": "source_file", "epoch": 12345, "verdict": "keep"}},
  {{"file": "source_file", "epoch": 12346, "verdict": "archive"}},
  {{"file": "source_file", "epoch": 12347, "verdict": "fuse_with", "fuse_target_epoch": 12345, "fused_content": "Combined content..."}}
], "description": "Brief description of the merged topic"}}
/no_think"""


def build_llm_merge_prompt(cluster: list[str], store) -> str:
    """Build LLM prompt for ambiguous merge decisions."""
    from core.memory.format import parse_entries_from_markdown

    parts = []
    for idx, name in enumerate(cluster, 1):
        md = store.read_file(name)
        if not md:
            continue
        entries = parse_entries_from_markdown(name, md)
        for entry in entries:
            content_preview = entry.content[:1024]
            parts.append(f'{idx}. "{name}" (epoch={entry.epoch})\n' f"   Content: {content_preview}")

    file_entries = "\n".join(parts)
    return _LLM_MERGE_PROMPT.format(file_entries=file_entries)


def parse_llm_merge_response(
    text: str,
    cluster: list[str],
) -> MergeDecision | None:
    """Parse LLM JSON response into a MergeDecision."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM merge response: %s", text[:200])
        return None

    target = data.get("target")
    if not target or target not in cluster:
        # Pick the first one mentioned or the first in cluster
        target = cluster[0]

    entries_to_keep = []
    entries_to_archive = []
    fused_entries = []

    for e in data.get("entries", []):
        file_name = e.get("file", "")
        epoch = e.get("epoch", 0)
        verdict = e.get("verdict", "keep").lower()

        if verdict == "keep":
            entries_to_keep.append((file_name, epoch))
        elif verdict == "archive":
            entries_to_archive.append((file_name, epoch))
        elif "fuse" in verdict:
            fuse_target = e.get("fuse_target_epoch", epoch)
            fused_content = e.get("fused_content", "")
            if fused_content:
                fused_entries.append(
                    {
                        "file": file_name,
                        "epoch": epoch,
                        "fuse_target_epoch": fuse_target,
                        "fused_content": fused_content,
                    }
                )
            else:
                entries_to_keep.append((file_name, epoch))

    source_files = [n for n in cluster if n != target]
    description = data.get("description", target.replace("_", " ").title())

    return MergeDecision(
        target_file=target,
        source_files=source_files,
        strategy="llm",
        entries_to_keep=entries_to_keep,
        entries_to_archive=entries_to_archive,
        fused_entries=fused_entries if fused_entries else None,
        target_description=description,
        reason=f"LLM merge: {len(cluster)} files consolidated by AI curator",
    )


# ---------------------------------------------------------------------------
# Merge execution
# ---------------------------------------------------------------------------


def execute_merge(store, decision: MergeDecision) -> dict:
    """Execute a merge decision. Returns stats dict.

    Moves kept entries to target, archives source files, logs to DB.
    """
    from core.memory.format import format_entry, parse_entries_from_markdown
    from db.database import connect_memory

    stats = {"entries_kept": 0, "entries_archived": 0, "entries_fused": 0}

    # 1. Move kept entries from source files to target
    for file_name, epoch in decision.entries_to_keep:
        if file_name == decision.target_file:
            stats["entries_kept"] += 1
            continue  # Already in target
        moved = store.move_entries(file_name, decision.target_file, [epoch])
        stats["entries_kept"] += moved

    # 2. Handle fused entries (LLM-produced merged content)
    if decision.fused_entries:
        for fused in decision.fused_entries:
            fused_content = fused.get("fused_content", "")
            fuse_target_epoch = fused.get("fuse_target_epoch", fused.get("epoch", 0))
            source_epoch = fused.get("epoch", 0)
            source_file = fused.get("file", "")

            if not fused_content:
                continue

            # Use the oldest epoch from the fused entries
            oldest_epoch = min(fuse_target_epoch, source_epoch)
            fused_epochs = sorted(set([fuse_target_epoch, source_epoch]))

            # Gather hit counts from both contributing entries
            total_hits = 0
            max_last_hit = 0
            conn = connect_memory()
            try:
                for src_file, src_epoch in [(source_file, source_epoch), (decision.target_file, fuse_target_epoch)]:
                    hit_row = conn.execute(
                        "SELECT hit_count, last_hit_at FROM memory_hits " "WHERE file_name = ? AND epoch = ?",
                        (src_file, str(src_epoch)),
                    ).fetchone()
                    if hit_row:
                        total_hits += hit_row["hit_count"]
                        max_last_hit = max(max_last_hit, hit_row["last_hit_at"])
            finally:
                conn.close()

            # Add fused entry to target
            store.add_entry(
                content=fused_content,
                file_name=decision.target_file,
                entry_type="finding",
                epoch=oldest_epoch,
                source="consolidate",
            )

            # Write summed hit count for fused entry
            if total_hits > 0:
                conn = connect_memory()
                try:
                    conn.execute(
                        "INSERT INTO memory_hits (file_name, epoch, hit_count, last_hit_at) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(file_name, epoch) DO UPDATE SET "
                        "hit_count = ?, last_hit_at = ?",
                        (decision.target_file, str(oldest_epoch), total_hits, max_last_hit, total_hits, max_last_hit),
                    )
                    conn.commit()
                finally:
                    conn.close()

            stats["entries_fused"] += 1

    # 3. Archive source files
    for source in decision.source_files:
        store.archive_file(source)

    stats["entries_archived"] = len(decision.entries_to_archive)

    # 4. Log to consolidation_log
    conn = connect_memory()
    try:
        conn.execute(
            "INSERT INTO consolidation_log "
            "(target_file, source_files, strategy, entries_kept, entries_archived, reason, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                decision.target_file,
                json.dumps(decision.source_files),
                decision.strategy,
                stats["entries_kept"] + stats["entries_fused"],
                stats["entries_archived"],
                decision.reason,
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "Consolidation complete: %s ← %s (%s) | kept=%d archived=%d fused=%d",
        decision.target_file,
        decision.source_files,
        decision.strategy,
        stats["entries_kept"],
        stats["entries_archived"],
        stats["entries_fused"],
    )
    return stats
