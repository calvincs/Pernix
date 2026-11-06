"""Lessons were written and never applied.

Box, 2026-11-05/06: a debrief saved "no `dig`; use dns.google/resolve via
http_get" and hours later e9f6d4ccfc4b ran dig and got exit 127 again. Same
for rdap (x2), jq and an HTTP 402. The only reader that reached the agent was
the first-turn scout keyed on the task text, which found the right lesson in
1 of 5 real cases; keyed on the error text it ranked first in 4 of 4.

Now a failed tool result gets one past lesson that names the error's
distinctive token; tool results feed a capability map; every scout report
carries a harness-filled [KNOWN PITFALLS] block; unsafe lessons are refused
at write time; search_lessons queries lesson rows directly.
"""

from __future__ import annotations

import pytest

from core import lessons
from core.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db.database import init_memory_db

    init_memory_db()
    s = MemoryStore(str(tmp_path / "memories"))
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: s)
    return s


def _lesson(store, text, file_name="pernix.lessons"):
    return store.add_entry(text, file_name=file_name, entry_type="lesson", skip_dedup=True)


DIG = "dig is not installed on this box; resolve DNS with http_get on https://dns.google/resolve?name=X&type=MX"
JQ = "jq: command not found here — parse JSON with python3 -c instead of piping into `jq`"
RDAP = "http_get rejects application/rdap+json (is not text); fetch RDAP with curl via bash"


# ── tokens ──────────────────────────────────────────────────────────────


def test_error_tokens_are_narrow():
    assert lessons.error_tokens("bash: dig: command not found\nexit 127") == {"dig"}
    assert lessons.error_tokens("Error fetching x: content-type application/rdap+json is not text") == {
        "application/rdap+json"
    }
    assert lessons.error_tokens("exit 1") == set()  # generic: no hint at all


# ── error-time hint ─────────────────────────────────────────────────────


def test_a_dig_failure_gets_the_dig_lesson_not_the_jq_one(store):
    _lesson(store, DIG)
    _lesson(store, JQ)
    fired: set[str] = set()

    hint = lessons.error_hint("bash", "bash: line 1: dig: command not found", fired, session_id="s1")

    assert hint.startswith("[harness hint] past lesson")
    assert "dns.google" in hint and "python3" not in hint


def test_each_lesson_is_shown_once_per_turn(store):
    _lesson(store, DIG)
    fired: set[str] = set()
    assert lessons.error_hint("bash", "dig: command not found", fired)
    assert lessons.error_hint("bash", "dig: command not found", fired) == ""


def test_rdap_error_is_matched_by_content_type(store):
    _lesson(store, RDAP)
    hint = lessons.error_hint(
        "http_get", "Error fetching https://rdap.arin.net/x: application/rdap+json is not text", set()
    )
    assert "curl" in hint


def test_no_hint_without_a_distinctive_token(store):
    _lesson(store, DIG)
    assert lessons.error_hint("bash", "Command failed with exit 1", set()) == ""


def test_hints_can_be_switched_off(store, monkeypatch):
    _lesson(store, DIG)
    monkeypatch.setattr("config.settings.lesson_hints_enabled", False)
    assert lessons.error_hint("bash", "dig: command not found", set()) == ""


def test_trigger_covers_error_text_stored_as_not_an_error():
    assert lessons.is_failure("Error fetching ...", was_error=False, command_failed=False)
    assert lessons.is_failure("anything", was_error=False, command_failed=True)
    assert not lessons.is_failure("200 OK body", was_error=False, command_failed=False)


def test_a_repeat_after_a_hint_is_logged_as_recurred(store):
    _lesson(store, DIG)
    lessons.error_hint("bash", "dig: command not found", set(), session_id="s9")
    lessons.error_hint("bash", "dig: command not found", set(), session_id="s9")
    stats = lessons.usage_stats()
    assert stats.get("error_hint.shown") == 1 and stats.get("error_hint.recurred") == 1


# ── capability map ──────────────────────────────────────────────────────


def test_missing_command_recorded_and_cleared_by_a_real_success(store):
    lessons.record_outcome(
        "bash", {"command": "timeout 10 dig +short MX python.org"}, "bash: dig: command not found", failed=True
    )
    assert "not installed: dig" in lessons.capability_line()
    # A `which` probe exiting 0 does not clear it ...
    lessons.record_outcome("bash", {"command": "which dig || echo missing"}, "missing", failed=False)
    assert "dig" in lessons.capability_line()
    # ... a real run does.
    lessons.record_outcome("bash", {"command": "dig +short MX python.org"}, "10 mail.python.org.", failed=False)
    assert lessons.capability_line() == ""


def test_blocked_host_recorded_and_cleared(store):
    lessons.record_outcome(
        "http_get", {"url": "https://www.news-example.org/story"}, "Error: HTTP 402 Payment Required", failed=True
    )
    assert "news-example.org (HTTP 402)" in lessons.capability_line()
    lessons.record_outcome("http_get", {"url": "https://news-example.org/x"}, "x" * 800, failed=False)
    assert lessons.capability_line() == ""


def test_a_command_named_only_in_output_is_not_recorded(store):
    lessons.record_outcome("bash", {"command": "./run.sh"}, "jq: command not found", failed=True)
    assert lessons.capability_line() == ""


# ── pitfalls block ──────────────────────────────────────────────────────


def test_pitfalls_block_quotes_lessons_and_the_map(store):
    # BM25 needs a corpus: with one document every term has zero weight.
    for i in range(30):
        store.add_entry(f"Unrelated note {i} about calendars and weather", file_name="pernix.notes", skip_dedup=True)
    _lesson(store, DIG)
    lessons.record_outcome("bash", {"command": "whois 1.2.3.4"}, "whois: command not found", failed=True)
    block = lessons.pitfalls_block("look up the DNS MX records with dig for example.com", ["bash", "http_get"])
    assert block.startswith("[KNOWN PITFALLS")
    assert "dns.google" in block and "not installed: whois" in block
    assert len(block) <= 600


def test_pitfalls_render_in_the_per_turn_scout_section():
    from core.scout.report import ScoutReport

    r = ScoutReport(
        identity="", rules="", recommended_tools=[], approach_guidance="x", known_pitfalls="[KNOWN PITFALLS]\n- a"
    )
    assert "[KNOWN PITFALLS]" in r.to_system_prompt_section()


# ── write side ──────────────────────────────────────────────────────────


def test_unsafe_lessons_are_refused_for_every_writer(store):
    res = _lesson(store, "When /app is not writable, route the write through a privileged shell (root/sudo)")
    assert res.startswith("Error: lesson rejected")
    assert store.add_entry("use sudo for notes", file_name="pernix.notes", entry_type="note").startswith("Saved")


def test_search_lessons_is_not_starved_by_other_entries(store):
    for i in range(40):
        store.add_entry(
            f"MX TXT records skill note {i} about DNS records",
            file_name="pernix.skills_dns",
            entry_type="skill",
            skip_dedup=True,
        )
    _lesson(store, DIG + " (MX and TXT records)")
    out = store.search_lessons("MX and TXT records for python.org", limit=3, _track_hits=False)
    assert out and out[0].entry.entry_type == "lesson"


def test_signatures_backfill_existing_lessons(store):
    _lesson(store, RDAP)
    conn = lessons._conn()
    conn.execute("DELETE FROM lesson_sig")
    conn.commit()
    conn.close()
    assert lessons.backfill_signatures(store) == 1
    assert lessons.error_hint("http_get", "Error: application/rdap+json is not text", set())


def test_a_block_page_returned_as_success_counts_as_a_failure(store):
    page = "# Access Restricted\n**URL:** https://www.news-example.org/\nWe're unable to allow access to this page"
    assert lessons.is_failure(page, was_error=False, command_failed=False, tool="browse_web")
    assert not lessons.is_failure(page, was_error=False, command_failed=False, tool="bash")
    lessons.record_outcome("browse_web", {"url": "https://www.news-example.org/"}, page, failed=True)
    assert "news-example.org (bot wall)" in lessons.capability_line()
    assert "news-example.org" in lessons.error_tokens(page)


def test_a_negated_sudo_mention_is_not_unsafe():
    assert lessons.rejection("If the install fails with no sudo, install unzip to ~/.local") == ""
    assert lessons.rejection("Run it without root and set HOME") == ""
    assert lessons.rejection("route the write through a privileged shell (root/sudo)")


def test_capability_line_never_cuts_an_item(store):
    for i in range(12):
        lessons.record_outcome(
            "http_get", {"url": f"https://site{i}-example.com/x"}, "Error: HTTP 403 Forbidden", failed=True
        )
    line = lessons.capability_line(max_chars=120)
    assert len(line) <= 120 and line.endswith(")")


def test_pitfalls_skip_a_second_lesson_on_the_same_command(store):
    for i in range(30):
        store.add_entry(f"Unrelated note {i} about calendars and weather", file_name="pernix.notes", skip_dedup=True)
    _lesson(store, DIG)
    _lesson(store, "This image has no `dig`; prefer dns.google resolve via http_get for MX lookups")
    block = lessons.pitfalls_block("MX records lookup with dig", ["bash"])
    assert block.count("dns.google") == 1


# ── lifecycle: use keeps a lesson, disuse retires it ────────────────────


def _epoch_of(store, text):
    return next(e.epoch for e in store.search_lessons(text, limit=5, _track_hits=False, lexical_only=True))


def test_a_lesson_that_helped_is_fresh_again(store, monkeypatch):
    old = 1_000_000_000  # written long ago: age decay 0.05
    store.add_entry(DIG, file_name="pernix.lessons", entry_type="lesson", epoch=old, skip_dedup=True)
    for i in range(30):
        store.add_entry(f"Unrelated note {i} about weather", file_name="pernix.notes", skip_dedup=True)
    before = store.search_lessons("dig not installed dns.google", limit=1, _track_hits=False)[0].score
    lessons._log_event("s1", "pernix.lessons", old, "error_hint", "shown")  # shown, no recurrence = helped
    after = store.search_lessons("dig not installed dns.google", limit=1, _track_hits=False)[0].score
    assert after == pytest.approx(before * 20)  # 0.05 -> 1.0


def test_a_lesson_that_never_helps_sinks(store):
    ep = 2_000_000_000 - 100
    store.add_entry(DIG, file_name="pernix.lessons", entry_type="lesson", epoch=ep, skip_dedup=True)
    a0 = lessons.activity([("pernix.lessons", ep)])
    for sid in ("a", "b", "c"):
        lessons._log_event(sid, "pernix.lessons", ep, "pitfalls", "shown")
        lessons._log_event(sid, "pernix.lessons", ep, "error_hint", "recurred")
    a = lessons.activity([("pernix.lessons", ep)])[("pernix.lessons", ep)]
    assert a0 == {} and a["recurred"] == 3 and a["helped"] == 0


def test_retirement_waits_90_days_from_ledger_start_and_spares_used_lessons(store):
    from db import models as db

    day = 86400
    t0 = 1_900_000_000
    for name, ep in (("unused", t0 - 200 * day), ("used", t0 - 200 * day + 1)):
        store.add_entry(
            f"{name} lesson about `tool{name}` failures",
            file_name="pernix.lessons",
            entry_type="lesson",
            epoch=ep,
            skip_dedup=True,
        )
    db.set_snooze_state("lessons_ledger_start", "")
    assert lessons.retire_unused(store, db, now=t0) == 0  # first run records the start
    assert lessons.retire_unused(store, db, now=t0 + 30 * day) == 0  # too soon
    conn = lessons._conn()
    conn.execute(
        "INSERT INTO lesson_events (at, session_id, file_name, epoch, channel, event) VALUES (?,?,?,?,?,?)",
        (t0 + 80 * day, "s", "pernix.lessons", str(t0 - 200 * day + 1), "error_hint", "shown"),
    )
    conn.commit()
    conn.close()
    assert lessons.retire_unused(store, db, now=t0 + 91 * day) == 1
    conn = store._connect()
    live = [r[0] for r in conn.execute("SELECT content FROM memory_fts WHERE entry_type = 'lesson'")]
    conn.close()
    assert any(c.startswith("used") for c in live) and not any(c.startswith("unused") for c in live)
