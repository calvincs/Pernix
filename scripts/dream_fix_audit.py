#!/usr/bin/env python3
"""Pernix dream_fix audit — list (and, on request, remove) corrective notes.

Usage:
    python scripts/dream_fix_audit.py                 # dry run: full listing
    python scripts/dream_fix_audit.py --json          # machine-readable
    python scripts/dream_fix_audit.py --apply --yes   # delete the REMOVE bucket

Why this exists: until dec216f the dream judge validated every dated market
snapshot as a contradiction/stale-memory finding, and each one that cleared
the veto window wrote a weight-high "overrides older entries" note into a
memory file (116 notes on the box when this shipped; an earlier raw-text grep overcounted at 215). Those
notes outrank the data they "correct" at retrieval time, so the pollution is
not cosmetic. The originating hypotheses are mostly status=promoted — a
terminal state the re-adjudication requeue never touches — so the notes are
classified here by CONTENT, not provenance:

    REMOVE  the note corrects a dated market snapshot: it names a market
            quantity AND carries snapshot evidence (a $ amount, an index
            level, a percentage). The time-series rule says these were never
            memory defects.
    KEEP    everything else — procedure conflicts, source-reliability
            corrections, schedule changes. The class dream is FOR.

Origin hypothesis status is shown for context (the note text embeds
dream:<id12>). A REVIEW flag marks notes whose origin is refuted/expired
after re-adjudication — corroboration, not the classifier.

Dry run is read-only. --apply deletes exactly the REMOVE bucket via
store.delete_entry (entry-level, FTS-index-aware) and requires --yes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Ensure repo root is on sys.path when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A market/time-varying quantity by name...
MARKET_QUANTITY_RE = re.compile(
    r"(?i)\b(S&P\s*500|S&P|NASDAQ|DJIA|Dow(?:\s+Jones)?|Nikkei|FTSE|Russell|VIX|Brent|WTI|crude|oil price|"
    r"10Y|treasury yield|bond yield|gold price|bitcoin|BTC|exchange rate)\b"
)
# ...with point-in-time evidence attached: a dollar amount, a 4-5 digit index
# level (7,259.22), a percentage move, or an all-time-high claim.
SNAPSHOT_EVIDENCE_RE = re.compile(r"(?i)(\$\s?\d|\b\d{1,2},\d{3}(\.\d+)?\b|\b\d+(\.\d+)?%|\ball-time high|record high)")
ORIGIN_REF_RE = re.compile(r"dream:([0-9a-f]{6,12})")


def classify(content: str) -> str:
    """REMOVE only when both signals are present.

    Quantity name alone is not enough: "the VIX source recommendation
    conflicts between FRED and Yahoo" is a source-reliability finding — a
    keeper — and it names VIX. The snapshot evidence (a level, a price) is
    what separates a record of the market from a claim about the system.
    """
    if MARKET_QUANTITY_RE.search(content) and SNAPSHOT_EVIDENCE_RE.search(content):
        return "REMOVE"
    return "KEEP"


def _origin_status(db, content: str) -> tuple[str, str]:
    m = ORIGIN_REF_RE.search(content)
    if not m:
        return "", "no-ref"
    prefix = m.group(1)
    rows = db.list_dream_hypotheses(limit=2000)
    for r in rows:
        if str(r.get("id", "")).startswith(prefix):
            return prefix, str(r.get("status", "unknown"))
    return prefix, "not-found"


def collect(store, db) -> list[dict]:
    from core.memory.format import parse_entries_from_markdown

    notes: list[dict] = []
    for mf in store.list_files():
        name = getattr(mf, "name", None) or str(mf)
        md = store.read_file(name)
        if not md:
            continue
        for e in parse_entries_from_markdown(name, md):
            if str(getattr(e, "source", "") or "") != "dream_fix":
                continue
            origin_id, origin_status = _origin_status(db, e.content)
            notes.append(
                {
                    "file": name,
                    "epoch": e.epoch,
                    "verdict": classify(e.content),
                    "origin_id": origin_id,
                    "origin_status": origin_status,
                    "content": " ".join(e.content.split())[:300],
                }
            )
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--apply", action="store_true", help="delete the REMOVE bucket")
    ap.add_argument("--yes", action="store_true", help="required with --apply")
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="HYP_PREFIX",
        help="origin-hypothesis id prefix to reclassify as KEEP (repeatable). "
        "For the cases regex cannot see: same-date disagreements and "
        "internally-impossible series, which the time-series rule keeps.",
    )
    args = ap.parse_args()

    from core.memory.store import get_memory_store
    from db import models as db

    store = get_memory_store()
    if store is None:
        print("no memory store configured", file=sys.stderr)
        return 1

    notes = collect(store, db)
    for n in notes:
        if n["verdict"] == "REMOVE" and any(
            n["origin_id"].startswith(x) or x.startswith(n["origin_id"]) for x in args.exclude if x
        ):
            n["verdict"] = "KEEP"
    remove = [n for n in notes if n["verdict"] == "REMOVE"]
    keep = [n for n in notes if n["verdict"] == "KEEP"]

    if args.json:
        print(json.dumps({"total": len(notes), "remove": remove, "keep": keep}, indent=1))
    else:
        for bucket, rows in (("REMOVE", remove), ("KEEP", keep)):
            print(f"\n===== {bucket} ({len(rows)}) =====")
            for n in rows:
                flag = (
                    " [origin now " + n["origin_status"] + "]" if n["origin_status"] in ("refuted", "expired") else ""
                )
                print(f"  {n['file']} @ {n['epoch']}{flag}")
                print(f"      {n['content'][:220]}")
        print(f"\nTotal dream_fix notes: {len(notes)}  REMOVE: {len(remove)}  KEEP: {len(keep)}")

    if args.apply:
        if not args.yes:
            print("\n--apply requires --yes (this deletes entries)", file=sys.stderr)
            return 2
        deleted = 0
        for n in remove:
            result = store.delete_entry(n["file"], n["epoch"])
            ok = not (isinstance(result, str) and result.startswith("Error"))
            deleted += ok
            if not ok:
                print(f"  FAILED {n['file']} @ {n['epoch']}: {result}", file=sys.stderr)
        print(f"deleted {deleted}/{len(remove)} REMOVE notes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
