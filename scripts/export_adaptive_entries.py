#!/usr/bin/env python3
"""Export the live adaptive entries before the adaptive layer is retired.

Usage:
    python scripts/export_adaptive_entries.py
    python scripts/export_adaptive_entries.py --db data/sessions.db --out data/adaptive/ACTIVE-AT-RETIREMENT.md

The adaptive layer (entries, proposals, trials, tripwire) was retired in 3.2.
Its tables stay in the database as history, but nothing reads them any more.
This writes one human-readable snapshot of what was live at retirement:

- every `active` and `trial` entry (id, kind, scope, source, status,
  version, title, content, created_at), ordered by kind then id;
- the first evidence recorded for it (the `create` event);
- its use / success / failure counts (scout_signals, type `adaptive_entry`);
- the canary specs still waiting in the proposal queue.

Raw sqlite3 only — it never imports `core.adaptive`, so it works before and
after the package is deleted. Read-only against the database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = _ROOT / "data" / "sessions.db"
DEFAULT_OUT = _ROOT / "data" / "adaptive" / "ACTIVE-AT-RETIREMENT.md"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _fmt_json(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw)


def collect(conn: sqlite3.Connection) -> dict:
    """Read everything the snapshot needs. Missing tables read as empty."""
    conn.row_factory = sqlite3.Row
    entries: list[dict] = []
    specs: list[dict] = []
    if _table_exists(conn, "adaptive_entries"):
        rows = conn.execute(
            "SELECT id, kind, scope, source, status, version, title, content, created_at "
            "FROM adaptive_entries WHERE status IN ('active', 'trial') ORDER BY kind, id"
        ).fetchall()
        has_events = _table_exists(conn, "adaptive_events")
        has_signals = _table_exists(conn, "scout_signals")
        for r in rows:
            e = dict(r)
            e["evidence"] = None
            if has_events:
                ev = conn.execute(
                    "SELECT evidence_json FROM adaptive_events WHERE entry_id = ? AND action = 'create' "
                    "ORDER BY id ASC LIMIT 1",
                    (r["id"],),
                ).fetchone()
                e["evidence"] = ev["evidence_json"] if ev else None
            e["uses"] = e["successes"] = e["failures"] = 0
            if has_signals:
                sig = conn.execute(
                    "SELECT reinforcements, successes, failures FROM scout_signals "
                    "WHERE signal_type = 'adaptive_entry' AND subject = ?",
                    (r["id"],),
                ).fetchone()
                if sig:
                    e["uses"] = sig["reinforcements"] or 0
                    e["successes"] = sig["successes"] or 0
                    e["failures"] = sig["failures"] or 0
            entries.append(e)
    if _table_exists(conn, "adaptive_proposals"):
        specs = [
            dict(r)
            for r in conn.execute(
                "SELECT id, payload_json, rationale, created_at FROM adaptive_proposals "
                "WHERE producer = 'canary_propose' AND status = 'pending' ORDER BY id"
            ).fetchall()
        ]
    return {"entries": entries, "canary_specs": specs}


def render(data: dict, *, db_path: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = data["entries"]
    specs = data["canary_specs"]
    out = [
        "# Adaptive entries live at retirement",
        "",
        f"Exported {now} from `{db_path}`.",
        "",
        "The adaptive layer was retired in Pernix 3.2. Its tables stay in the",
        "database as history; nothing renders these entries into prompts any more.",
        "",
        f"Live entries: {len(entries)}. Pending canary specs: {len(specs)}.",
        "",
    ]
    kind = None
    for e in entries:
        if e["kind"] != kind:
            kind = e["kind"]
            out += [f"## {kind}", ""]
        out += [
            f"### {e['title']}",
            "",
            f"- id: `{e['id']}`",
            f"- scope: {e['scope']}",
            f"- source: {e['source']}",
            f"- status: {e['status']}",
            f"- version: {e['version']}",
            f"- created_at: {e['created_at'] or ''}",
            f"- uses: {e['uses']} (successes {e['successes']}, failures {e['failures']})",
            "",
            e["content"] or "",
            "",
        ]
        ev = _fmt_json(e.get("evidence"))
        if ev:
            out += ["First evidence:", "", "```json", ev, "```", ""]
    if specs:
        out += ["## Pending canary specs", ""]
        for s in specs:
            out += [f"### Proposal {s['id']} ({s['created_at'] or ''})", ""]
            if s.get("rationale"):
                out += [s["rationale"], ""]
            out += ["```json", _fmt_json(s["payload_json"]), "```", ""]
    return "\n".join(out).rstrip() + "\n"


def export(db_path: Path, out_path: Path) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        data = collect(conn)
    finally:
        conn.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(data, db_path=str(db_path)), encoding="utf-8")
    return {"entries": len(data["entries"]), "canary_specs": len(data["canary_specs"]), "out": str(out_path)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", default=str(DEFAULT_DB), help="sessions database (default data/sessions.db)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="markdown file to write")
    args = ap.parse_args(argv)
    try:
        res = export(Path(args.db), Path(args.out))
    except FileNotFoundError as exc:
        print(f"database not found: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {res['entries']} entries and {res['canary_specs']} canary specs to {res['out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
