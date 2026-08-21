#!/usr/bin/env python3
"""Relabel corrective memory entries whose provenance says "human-approved"
but whose proposal was resolved by the veto-window drain.

Before fdbe0a8 (2026-08-21) every correction written by approve_proposal said
"human-approved via adaptive review" — including the ones auto_approved by
auto_approve_stale_proposals. This walks the auto_approved memory-correction
proposals, finds each corrective entry by its `dream:<hypothesis>` tag in the
cited memory files, and rewrites the preamble to what correction_preamble()
writes today. Dry run by default; --apply writes through the store so FTS and
vectors stay consistent. Run inside the container:

    docker exec -w /app pernix python scripts/relabel_auto_approved_corrections.py [--apply]
"""

from __future__ import annotations

import argparse
import json
import re
import sys

sys.path.insert(0, ".")

from core.memory.ingest import correction_preamble  # noqa: E402
from core.memory.store import get_memory_store  # noqa: E402
from db import models as db  # noqa: E402

_OLD = re.compile(
    r"^(STALE-INFO CORRECTION|CONTRADICTION RESOLVED) \(human-approved via adaptive review, (dream:[0-9a-f]+)\)"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the relabelled entries (default: dry run)")
    args = ap.parse_args()
    store = get_memory_store()
    planned: list[tuple[str, int, str, str]] = []
    for prop in db.adaptive_list_proposals(status="auto_approved", limit=1000):
        try:
            edits = json.loads(prop.get("payload_json") or "[]")
        except (TypeError, ValueError):
            continue
        for e in edits:
            if not isinstance(e, dict) or e.get("action") != "memory_correction":
                continue
            tag = f"dream:{str(e.get('hypothesis_id') or '')[:12]}"
            kind = str(e.get("kind") or "contradiction")
            for fname in e.get("files") or []:
                md = store.read_file(fname) or ""
                if tag not in md:
                    continue
                from core.memory.format import parse_entries_from_markdown

                for entry in parse_entries_from_markdown(fname, md):
                    m = _OLD.match(entry.content)
                    if not m or m.group(2) != tag:
                        continue
                    new_preamble = correction_preamble(kind, "auto", tag)
                    new_content = new_preamble + entry.content[m.end() :]
                    planned.append((fname, entry.epoch, entry.content[:90], new_content))
    print(
        f"{len(planned)} corrective entr{'y' if len(planned) == 1 else 'ies'} labelled human-approved but drained automatically"
    )
    for fname, epoch, old, new in planned:
        print(f"- {fname}@{epoch}: {old!r}")
        print(f"    -> {new[:110]!r}")
    if not args.apply:
        print("dry run — re-run with --apply to write")
        return 0
    done = 0
    for fname, epoch, _old, new in planned:
        result = store.update_entry(fname, epoch, new)
        ok = isinstance(result, str) and not result.startswith("Error")
        print(f"  {'OK ' if ok else 'ERR'} {fname}@{epoch}: {result if not ok else 'updated'}")
        done += ok
    print(f"relabelled {done}/{len(planned)}")
    return 0 if done == len(planned) else 1


if __name__ == "__main__":
    raise SystemExit(main())
