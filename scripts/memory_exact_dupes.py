#!/usr/bin/env python3
"""Pernix memory exact-duplicate audit — list (and, on request, remove) copies.

Usage:
    python scripts/memory_exact_dupes.py                 # dry run
    python scripts/memory_exact_dupes.py --json          # machine-readable
    python scripts/memory_exact_dupes.py --apply --yes   # delete redundant copies

The write-side gate (store._find_exact, added the same day) stops NEW exact
duplicates; this removes the ones already stored — 409 redundant copies in
263 groups on the box when it shipped, 371 of them written by distill with
epochs seconds apart (the class dream flagged as a data-ingestion bug).

Scope is deliberately narrow: entries in the SAME file whose whitespace-
normalized content is identical. The OLDEST copy in each group is kept (the
original; its epoch is what wiki-links and dream evidence pin against) and
the later copies are deleted. Near-duplicates are none of this script's
business — they belong to the similarity gate and the consolidation pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

# Ensure repo root is on sys.path when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def collect(store) -> list[dict]:
    from core.memory.format import parse_entries_from_markdown

    groups: list[dict] = []
    for mf in store.list_files():
        name = getattr(mf, "name", None) or str(mf)
        md = store.read_file(name)
        if not md:
            continue
        by_hash: dict[str, list] = defaultdict(list)
        for e in parse_entries_from_markdown(name, md):
            h = hashlib.sha256(" ".join(e.content.split()).encode("utf-8")).hexdigest()
            by_hash[h].append(e)
        for entries in by_hash.values():
            if len(entries) < 2:
                continue
            entries.sort(key=lambda e: e.epoch)
            groups.append(
                {
                    "file": name,
                    "keep_epoch": entries[0].epoch,
                    "delete_epochs": [e.epoch for e in entries[1:]],
                    "source": str(getattr(entries[0], "source", "") or ""),
                    "content": " ".join(entries[0].content.split())[:200],
                }
            )
    return groups


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--apply", action="store_true", help="delete the redundant copies")
    ap.add_argument("--yes", action="store_true", help="required with --apply")
    args = ap.parse_args()

    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        print("no memory store configured", file=sys.stderr)
        return 1

    groups = collect(store)
    redundant = sum(len(g["delete_epochs"]) for g in groups)

    if args.json:
        print(json.dumps({"groups": len(groups), "redundant": redundant, "detail": groups}, indent=1))
    else:
        for g in groups:
            print(f"  {g['file']}  keep @{g['keep_epoch']}  delete {g['delete_epochs']}  [{g['source']}]")
            print(f"      {g['content'][:180]}")
        print(f"\nGroups: {len(groups)}  redundant copies: {redundant}")

    if args.apply:
        if not args.yes:
            print("\n--apply requires --yes (this deletes entries)", file=sys.stderr)
            return 2
        deleted = 0
        for g in groups:
            for epoch in g["delete_epochs"]:
                result = store.delete_entry(g["file"], epoch)
                ok = not (isinstance(result, str) and result.startswith("Error"))
                deleted += ok
                if not ok:
                    print(f"  FAILED {g['file']} @ {epoch}: {result}", file=sys.stderr)
        print(f"deleted {deleted}/{redundant} redundant copies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
