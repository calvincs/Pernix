"""Pernix — Dream observe: assemble a small, labeled evidence pack.

Sources, each behind its own cursor so steps stay incremental:
  - post_mortems newer than snooze_state[dream_pm_cursor]
  - the Candor intel brief (semantic summary; parsed into per-line refs)
  - one memory file per step, rotated via snooze_state[dream_mem_cursor]

Every item gets a short ref id ([P1], [C1], [M1]...). The hypothesizer may
cite only these ids — fabricated references die at the parse boundary.
Memory refs carry a content-hash prefix so later validation can detect that
consolidation/splitting moved or rewrote the entry (stale ref => expired,
never guessed). Entries with source == "dream" are excluded from the sample:
the dreamer must not dream about its own output (self-reinforcement guard).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

from config import settings
from db import models as db

logger = logging.getLogger("pernix.dream.observe")

_PM_LIMIT = 6
_CANDOR_LINE_LIMIT = 6
_MEMORY_ENTRY_LIMIT = 12
_RENDER_CHAR_CAP = 300

# `- pred(arg1, arg2): 62% success over 41 obs ...` (intel.py line format)
_BRIEF_LINE_RE = re.compile(r"^- (\w+)\(([^)]*)\):")


def content_hash(text: str) -> str:
    """Stable short hash over whitespace-normalized content."""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


@dataclass
class EvidenceItem:
    ref_id: str  # "P1" | "C1" | "M1" ...
    kind: str  # "pm" | "candor" | "memory"
    render: str  # the line shown to the LLM (already ref-id prefixed)
    ref: dict  # machine ref for later validation


@dataclass
class EvidencePack:
    items: list[EvidenceItem] = field(default_factory=list)
    memory_file: str | None = None  # file sampled this step (cursor advance)
    pm_high_water: str | None = None  # max post-mortem created_at seen

    def refs_by_id(self) -> dict[str, EvidenceItem]:
        return {i.ref_id: i for i in self.items}

    def render(self) -> str:
        return "\n".join(i.render for i in self.items)


async def build_pack(store) -> EvidencePack:
    """Gather the evidence pack. Never raises; missing sources are skipped."""
    pack = EvidencePack()

    # --- Post-mortems since cursor ---------------------------------------
    try:
        cursor = db.get_snooze_state("dream_pm_cursor") or ""
        pms = db.list_post_mortems_since(cursor, limit=_PM_LIMIT)
        if pms:
            pack.pm_high_water = pms[-1]["created_at"]
        for i, pm in enumerate(pms, 1):
            try:
                payload = json.loads(pm.get("payload_json") or "{}")
            except (TypeError, ValueError):
                payload = {}
            # Canary isolation (plan §5): synthetic-task post-mortems must not
            # feed dream promotion evidence. Cursor already advanced above.
            if payload.get("session_type") == "canary":
                continue
            detail = str(payload.get("what_failed") or payload.get("diagnostic") or "").strip()
            render = (
                f"[P{i}] post-mortem {str(pm.get('created_at', ''))[:10]}: "
                f"verdict={pm.get('verdict')} cause={pm.get('failure_cause')} "
                f"confidence={float(pm.get('confidence') or 0.0):.2f}"
            )
            if detail:
                render += f" — {detail[:220]}"
            pack.items.append(
                EvidenceItem(
                    ref_id=f"P{i}",
                    kind="pm",
                    render=render,
                    ref={"id": pm.get("id"), "session_id": pm.get("session_id")},
                )
            )
    except Exception as e:
        logger.debug("dream observe: post-mortem gather failed: %s", e)

    # --- Candor intel brief (semantic summary of the outcome ledger) ------
    if settings.candor_enabled:
        try:
            from core.extensions.candor.bridge import get_candor_bridge

            brief = await get_candor_bridge().intel_brief()
        except Exception as e:
            logger.debug("dream observe: candor brief failed: %s", e)
            brief = None
        if brief:
            ci = 0
            for line in brief.splitlines():
                line = line.strip()
                m = _BRIEF_LINE_RE.match(line)
                if not m:
                    continue
                ci += 1
                args = [a.strip() for a in m.group(2).split(",") if a.strip()]
                pack.items.append(
                    EvidenceItem(
                        ref_id=f"C{ci}",
                        kind="candor",
                        render=f"[C{ci}] {line[2:][:_RENDER_CHAR_CAP]}",
                        ref={"pred": m.group(1), "args": args},
                    )
                )
                if ci >= _CANDOR_LINE_LIMIT:
                    break

    # --- One memory file, rotated ----------------------------------------
    try:
        from core.memory.format import parse_entries_from_markdown

        files = sorted(f.name for f in await asyncio.to_thread(store.list_files) if f.entry_count > 0)
        if files:
            cursor = db.get_snooze_state("dream_mem_cursor") or ""
            mem_file = next((n for n in files if n > cursor), files[0])
            md = await asyncio.to_thread(store.read_file, mem_file)
            if md:
                entries = [e for e in parse_entries_from_markdown(mem_file, md) if e.source != "dream"]
                now = time.time()
                for i, e in enumerate(entries[:_MEMORY_ENTRY_LIMIT], 1):
                    age_days = max(0, int((now - e.epoch) // 86400))
                    origin_tag = ", web-derived" if e.origin == "external" else ""
                    pack.items.append(
                        EvidenceItem(
                            ref_id=f"M{i}",
                            kind="memory",
                            render=(
                                f"[M{i}] (memory {mem_file}@{e.epoch}, {e.entry_type}, "
                                f"{age_days}d old{origin_tag}) {e.content[:_RENDER_CHAR_CAP]}"
                            ),
                            ref={"file": mem_file, "epoch": e.epoch, "hash": content_hash(e.content)},
                        )
                    )
            pack.memory_file = mem_file
    except Exception as e:
        logger.debug("dream observe: memory gather failed: %s", e)

    return pack
