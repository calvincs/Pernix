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


def parse_metadata(content: str) -> tuple[dict[str, str], str]:
    """Extract @key: value metadata lines from entry start.

    Returns (metadata_dict, remaining_content).
    """
    metadata = {}
    lines = content.split("\n")
    body_start = 0
    for i, line in enumerate(lines):
        match = re.match(r"^@(\w+):\s*(.*)", line.strip())
        if match:
            metadata[match.group(1)] = match.group(2).strip()
            body_start = i + 1
        else:
            break
    body = "\n".join(lines[body_start:]).strip()
    return metadata, body


def format_entry(
    content: str,
    entry_type: str = "note",
    tags: str = "",
    weight: str = "normal",
    source: str = "",
    epoch: int | None = None,
    merged_from: str = "",
    fused_epochs: list[int] | None = None,
) -> str:
    """Format an entry for markdown file storage."""
    epoch = epoch or int(time.time())
    lines = [
        "",
        "---",
        f"<!-- @epoch: {epoch} -->",
        f"<!-- @type: {entry_type} -->",
    ]
    if tags:
        lines.append(f"<!-- @tags: {tags} -->")
    if weight == "high":
        lines.append(f"<!-- @weight: {weight} -->")
    if source:
        lines.append(f"<!-- @source: {source} -->")
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


def parse_entries_from_markdown(file_name: str, text: str) -> list[MemoryEntry]:
    """Parse all entries from a markdown memory file."""
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
                )
            )

    return entries
