"""scripts/export_adaptive_entries.py — the retirement snapshot of live adaptive entries.

Raw sqlite only, so the test builds the four tables by hand instead of going
through db.database (the script must keep working after core.adaptive and
its accessors are gone).
"""

from __future__ import annotations

import json
import sqlite3

from scripts.export_adaptive_entries import export, main


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE adaptive_entries (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'global',
            title TEXT NOT NULL, content TEXT NOT NULL, risk TEXT NOT NULL DEFAULT 'low',
            version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
            source TEXT NOT NULL, created_at TEXT, updated_at TEXT);
        CREATE TABLE adaptive_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL, action TEXT NOT NULL,
            before_json TEXT, after_json TEXT, evidence_json TEXT, actor TEXT,
            proposal_id INTEGER, batch_id TEXT, created_at TEXT);
        CREATE TABLE adaptive_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, producer TEXT NOT NULL, payload_json TEXT NOT NULL,
            evidence_json TEXT, rationale TEXT, status TEXT NOT NULL DEFAULT 'pending',
            resolved_at TEXT, created_at TEXT);
        CREATE TABLE scout_signals (
            signal_type TEXT NOT NULL, subject TEXT NOT NULL, reinforcements INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL, last_reinforced_at TEXT NOT NULL,
            user_approved INTEGER DEFAULT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (signal_type, subject));
    """)
    rows = [
        ("e-b", "routing_hint", "Bravo hint", "use grep first", "active", "refine"),
        ("e-a", "routing_hint", "Alpha hint", "read the skill", "trial", "refine"),
        ("e-c", "prompt_note", "Charlie note", "be brief", "active", "dream"),
        ("e-x", "prompt_note", "Retired note", "gone", "retired", "dream"),
    ]
    for eid, kind, title, content, status, source in rows:
        conn.execute(
            "INSERT INTO adaptive_entries (id, kind, title, content, status, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '2026-09-01T00:00:00+00:00')",
            (eid, kind, title, content, status, source),
        )
    conn.execute(
        "INSERT INTO adaptive_events (entry_id, action, evidence_json) VALUES ('e-b', 'create', ?)",
        (json.dumps({"session": "s-first"}),),
    )
    conn.execute(
        "INSERT INTO adaptive_events (entry_id, action, evidence_json) VALUES ('e-b', 'update', ?)",
        (json.dumps({"session": "s-later"}),),
    )
    conn.execute(
        "INSERT INTO scout_signals (signal_type, subject, reinforcements, successes, failures, "
        "first_seen_at, last_reinforced_at) VALUES ('adaptive_entry', 'e-b', 7, 5, 2, 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO adaptive_proposals (producer, payload_json, rationale, status) "
        "VALUES ('canary_propose', ?, 'covers the grep failure', 'pending')",
        (json.dumps({"name": "grep-spec"}),),
    )
    conn.execute(
        "INSERT INTO adaptive_proposals (producer, payload_json, status) VALUES ('canary_propose', '{}', 'rejected')"
    )
    conn.execute("INSERT INTO adaptive_proposals (producer, payload_json, status) VALUES ('refine', '{}', 'pending')")
    conn.commit()
    conn.close()


def test_export_writes_live_entries_evidence_signals_and_specs(tmp_path):
    db = tmp_path / "export-src.db"
    _make_db(db)
    out = tmp_path / "adaptive" / "ACTIVE-AT-RETIREMENT.md"

    res = export(db, out)

    assert res["entries"] == 3 and res["canary_specs"] == 1
    text = out.read_text()
    # active + trial only, ordered by kind then id
    assert "Retired note" not in text
    assert text.index("## prompt_note") < text.index("## routing_hint")
    assert text.index("Alpha hint") < text.index("Bravo hint")
    assert "status: trial" in text
    # first evidence is the create event, not later ones
    assert "s-first" in text and "s-later" not in text
    assert "uses: 7 (successes 5, failures 2)" in text
    # only pending canary specs
    assert "grep-spec" in text and "covers the grep failure" in text
    assert "Proposal 2" not in text and "Proposal 3" not in text


def test_export_tolerates_a_db_without_adaptive_tables(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    out = tmp_path / "out.md"
    assert export(db, out) == {"entries": 0, "canary_specs": 0, "out": str(out)}
    assert "Live entries: 0" in out.read_text()


def test_main_reports_a_missing_db(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "o.md")]) == 1
    assert "not found" in capsys.readouterr().err
