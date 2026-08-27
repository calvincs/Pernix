#!/usr/bin/env python3
"""Pernix adaptive narrative cleanup — one-shot sweep of the noise the
content lint now stops at the mouth.

Usage:
    python scripts/adaptive_cleanup_narratives.py                 # dry run
    python scripts/adaptive_cleanup_narratives.py --json          # machine-readable
    python scripts/adaptive_cleanup_narratives.py --apply --yes   # retire the REMOVE bucket

Why this exists: before the actionability gate + lint (v3.1), Dream's
lesson_ineffective channel copied raw hypothesis statements into `policy`
entries ("Despite high-confidence verifications, the agent repeatedly
fails to...") and TELOS wrapped diagnostic prose into routing hints —
14 of 21 live policy slots and several duplicate hints on the box. Their
producers can no longer mint that shape, but the standing entries would
otherwise sit in every compiled prompt until their 90-day TTLs. Buckets:

    REMOVE  active machine-authored entries whose content fails the new
            lint (core/adaptive/lint.py), plus telos routing hints whose
            normalized content duplicates an older telos hint.
    KEEP    everything else — including every human-authored entry
            (source=user), which is never judged.

Dry run is read-only. --apply soft-deletes exactly the REMOVE bucket via
engine.delete_entry (journaled with full snapshots — each deletion is
individually rollbackable from the Adaptive tab) and posts ONE aggregate
notification listing what went and how to undo it. Requires --yes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Ensure repo root is on sys.path when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _normalize(content: str) -> str:
    """Collapse a telos hint to its semantic core for duplicate detection."""
    text = re.sub(r"\(c_\d+[^)]*\)", "", content or "")
    text = re.sub(r"confidence \d\.\d+", "", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return " ".join(text.split())[:160]


def classify(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """(remove, keep) over ACTIVE entries. Human-authored is always KEEP."""
    from core.adaptive.lint import lint_edit

    remove: list[dict] = []
    keep: list[dict] = []
    seen_telos: dict[str, str] = {}  # normalized content -> keeper entry id
    for e in sorted(entries, key=lambda x: str(x.get("created_at") or "")):
        if e.get("status") != "active" or e.get("source") == "user":
            keep.append(e)
            continue
        reason = lint_edit(
            {"action": "create", "kind": e.get("kind"), "content": e.get("content"), "title": e.get("title")}
        )
        if reason:
            remove.append({**e, "why": f"lint: {reason}"})
            continue
        if e.get("source") == "telos" and e.get("kind") == "routing_hint":
            key = _normalize(str(e.get("content") or ""))
            if key in seen_telos:
                remove.append({**e, "why": f"duplicate of {seen_telos[key]} (same claim, reworded)"})
                continue
            seen_telos[key] = e["id"]
        keep.append(e)
    return remove, keep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--apply", action="store_true", help="retire the REMOVE bucket")
    ap.add_argument("--yes", action="store_true", help="required with --apply")
    args = ap.parse_args()

    from db import models as db

    entries = db.adaptive_list_entries(status="active", limit=200)
    remove, keep = classify(entries)

    if args.json:
        print(
            json.dumps(
                {
                    "remove": [{"id": e["id"], "kind": e["kind"], "source": e["source"], "why": e["why"]} for e in remove],
                    "keep": [e["id"] for e in keep],
                },
                indent=2,
            )
        )
    else:
        print(f"ACTIVE entries: {len(entries)} — REMOVE {len(remove)}, KEEP {len(keep)}\n")
        for e in remove:
            print(f"REMOVE [{e['kind']}|{e['source']}] {e['id']}")
            print(f"       {str(e.get('content') or '')[:110]}")
            print(f"       → {e['why']}\n")

    if not args.apply:
        print("(dry run — nothing changed; use --apply --yes to retire the REMOVE bucket)")
        return 0
    if not args.yes:
        print("--apply requires --yes", file=sys.stderr)
        return 2
    if not remove:
        print("nothing to retire")
        return 0

    from core.adaptive.engine import delete_entry

    retired: list[str] = []
    for e in remove:
        try:
            delete_entry(e["id"], actor="cleanup-script")
            retired.append(e["id"])
        except Exception as exc:  # keep sweeping — one refusal must not stop the pass
            print(f"failed to retire {e['id']}: {exc}", file=sys.stderr)
    try:
        db.add_notification(
            title=f"Adaptive cleanup: {len(retired)} narrative entr{'y' if len(retired) == 1 else 'ies'} retired",
            body=(
                "One-time sweep of pre-lint content (descriptive findings and duplicate hints "
                "that rendered into every prompt). Each deletion is journaled with a full "
                "snapshot — roll any of them back from the Adaptive tab.\n"
                + "\n".join(f"• {i}" for i in retired[:20])
                + (f"\n(+{len(retired) - 20} more)" if len(retired) > 20 else "")
            ),
            urgency="normal",
        )
    except Exception as exc:
        print(f"notification failed: {exc}", file=sys.stderr)
    print(f"retired {len(retired)}/{len(remove)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
