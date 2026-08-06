"""Pernix — Markdown entry parsing and rendering for memory files."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass


@dataclass
class MemoryEntry:
    """A single memory entry parsed from markdown."""

    file_name: str
    content: str
    epoch: int
    entry_type: str = "note"  # finding | decision | skill | note
    tags: list[str] = None
    weight: str = "normal"  # high | normal
    score: float = 0.0
    source: str = ""  # user | distill | snooze | "" (legacy)
    updated: int = 0  # set when the entry was corrected via update_entry
    origin: str = ""  # claim origin: external (web-derived) | internal | "" (unknown)

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


@dataclass
class MemoryFile:
    """Metadata for a memory file."""

    name: str
    description: str
    keywords: list[str]
    entry_count: int = 0
    created_at: int = 0
    updated_at: int = 0


# Wiki-links (H4, plan §12.5): [[file-name]] pulls that file's entries into
# recall; [[file-name@epoch]] pins one entry. File-keyed — memory entries
# have no titles, so [[entry-title]] waits until they do.
WIKILINK_RE = re.compile(r"\[\[([a-zA-Z0-9._-]{1,64}(?:@\d{1,12})?)\]\]")


def extract_links(content: str) -> list[str]:
    """Unique [[refs]] in order of first appearance. Never raises."""
    seen: list[str] = []
    for m in WIKILINK_RE.finditer(content or ""):
        ref = m.group(1)
        if ref not in seen:
            seen.append(ref)
    return seen


def sanitize_entry_content(content: str) -> str:
    """Neutralize bare ``---`` lines in entry content.

    Entries are separated by ``\\n---\\n`` in the markdown files; a bare
    horizontal rule inside an entry body splits it into an epoch-less
    fragment that silently drops on the next reindex. Rendered as
    ``- - -`` (still a markdown rule) so the visual intent survives.
    """
    return re.sub(r"(?m)^---$", "- - -", content)


def format_entry(
    content: str,
    entry_type: str = "note",
    tags: str = "",
    weight: str = "normal",
    source: str = "",
    epoch: int | None = None,
    merged_from: str = "",
    fused_epochs: list[int] | None = None,
    updated: int = 0,
    origin: str = "",
) -> str:
    """Format an entry for markdown file storage."""
    epoch = epoch or int(time.time())
    content = sanitize_entry_content(content)
    lines = [
        "",
        "---",
        f"<!-- @epoch: {epoch} -->",
        f"<!-- @type: {entry_type} -->",
    ]
    if tags:
        lines.append(f"<!-- @tags: {tags} -->")
    # Any non-default weight must roundtrip through markdown, or the next
    # reindex() silently resets it (markdown is source of truth).
    if weight and weight != "normal":
        lines.append(f"<!-- @weight: {weight} -->")
    if source:
        lines.append(f"<!-- @source: {source} -->")
    if origin:
        lines.append(f"<!-- @origin: {origin} -->")
    if updated:
        lines.append(f"<!-- @updated: {updated} -->")
    if merged_from:
        lines.append(f"<!-- @merged_from: {merged_from} -->")
        lines.append(f"<!-- @merged_at: {int(time.time())} -->")
    if fused_epochs:
        lines.append(f"<!-- @fused_epochs: {','.join(str(e) for e in fused_epochs)} -->")
        lines.append(f"<!-- @fused_at: {int(time.time())} -->")
    lines.append(content)
    lines.append("")
    return "\n".join(lines)


def format_file_header(name: str, description: str, keywords: list[str]) -> str:
    """Format the header for a new memory file."""
    epoch = int(time.time())
    kw_str = ", ".join(keywords)
    return f"""<!-- @file: {name} -->
<!-- @description: {description} -->
<!-- @keywords: {kw_str} -->
<!-- @created: {epoch} -->
"""


def is_file_archived(text: str) -> bool:
    """True if the file-level header carries the archived marker.

    archive_file() inserts ``<!-- @archived: true -->`` directly after the
    ``<!-- @file: ... -->`` line in the header — i.e. before the first
    ``\\n---\\n`` entry separator. Per-entry archived markers (snooze dedup)
    appear only inside entry sections and do not archive the file.
    """
    header = text.split("\n---\n", 1)[0]
    return "<!-- @file:" in header and "<!-- @archived: true -->" in header


def parse_entries_from_markdown(file_name: str, text: str) -> list[MemoryEntry]:
    """Parse all entries from a markdown memory file.

    An archived file (file-level header marker) has no live entries —
    without this check, reindex()/health_check would resurrect archived
    files into the FTS5 index.
    """
    if is_file_archived(text):
        return []

    entries = []
    # Split by --- separator
    sections = re.split(r"\n---\n", text)

    for section in sections:
        section = section.strip()
        if not section or section.startswith("<!-- @file:"):
            continue

        # Skip archived entries
        if "<!-- @archived: true -->" in section:
            continue

        # Extract epoch
        epoch_match = re.search(r"<!-- @epoch:\s*(\d+)\s*-->", section)
        if not epoch_match:
            continue
        epoch = int(epoch_match.group(1))

        # Extract metadata from HTML comments
        entry_type = "note"
        tags = []
        weight = "normal"
        source = ""

        type_match = re.search(r"<!-- @type:\s*(\w+)\s*-->", section)
        if type_match:
            entry_type = type_match.group(1)

        tags_match = re.search(r"<!-- @tags:\s*(.*?)\s*-->", section)
        if tags_match:
            tags = [t.strip() for t in tags_match.group(1).split(",") if t.strip()]

        weight_match = re.search(r"<!-- @weight:\s*(\w+)\s*-->", section)
        if weight_match:
            weight = weight_match.group(1)

        source_match = re.search(r"<!-- @source:\s*(\w+)\s*-->", section)
        if source_match:
            source = source_match.group(1)

        origin = ""
        origin_match = re.search(r"<!-- @origin:\s*(\w+)\s*-->", section)
        if origin_match:
            origin = origin_match.group(1)

        updated = 0
        updated_match = re.search(r"<!-- @updated:\s*(\d+)\s*-->", section)
        if updated_match:
            updated = int(updated_match.group(1))

        # Extract content (remove HTML comment lines)
        content_lines = []
        for line in section.split("\n"):
            if not line.strip().startswith("<!--"):
                content_lines.append(line)
        content = "\n".join(content_lines).strip()

        if content:
            entries.append(
                MemoryEntry(
                    file_name=file_name,
                    content=content,
                    epoch=epoch,
                    entry_type=entry_type,
                    tags=tags,
                    weight=weight,
                    source=source,
                    updated=updated,
                    origin=origin,
                )
            )

    return entries
