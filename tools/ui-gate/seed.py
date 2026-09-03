#!/usr/bin/env python3
"""Seed the mobile-audit app with realistic transcript shapes.
Run with cwd = the smoke appdir.  seed.py <repo>
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, sys.argv[1])
from db import models as db  # noqa: E402
from db.database import connect_sessions  # noqa: E402

CODE = """```python
def reconcile(ledger: dict[str, list[Entry]], *, tolerance: float = 0.005) -> ReconcileReport:
    \"\"\"Match every debit against its credit and report anything that drifts past tolerance.\"\"\"
    unmatched = {account: [e for e in entries if not e.matched] for account, entries in ledger.items()}
    return ReconcileReport(unmatched=unmatched, checked_at=datetime.now(timezone.utc).isoformat())
```"""
TABLE = """| Provider | Model | Context | Input $/M | Output $/M | Notes |
|---|---|---|---|---|---|
| OpenRouter | qwen/qwen3-27b | 128k | 0.20 | 0.60 | default |
| Ollama (aibox) | qwen3.8-27b-fp16 | 192k | 0 | 0 | local, 3 concurrent |
| Anthropic | claude-sonnet-5 | 200k | 3.00 | 15.00 | fallback only |"""
LONG = (
    "Here is what I found after reading the deploy script and the compose file. The container pulls a baked image, so a `git pull` alone deploys nothing; "
    "you have to rebuild. The admin endpoints are only reachable through `docker exec`, and the health check at https://pernix.example.internal/api/health?verbose=1&include=maintenance,snooze,subscribers "
    "returns the maintenance block you asked about.\n\n"
    + CODE
    + "\n\nAnd the provider table:\n\n"
    + TABLE
    + "\n\n1. Rebuild the image.\n2. Restart the container.\n3. Watch the log for `deploy-sweep`.\n\nA very long unbroken token for good measure: "
    "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


def asst(sid, text, ms=1800, model="qwen3-27b"):
    db.add_message(sid, "assistant", text, latency_ms=ms, metadata=json.dumps({"model": model, "latency_ms": ms}))


# --- the main chat: rich markdown + tool rounds -------------------------------
main = db.create_session(title="Deploy the new build to the box and verify the health endpoint")
db.add_message(
    main, "user", "Can you check the deploy script and tell me why a plain git pull does nothing on the box?"
)
asst(main, LONG, 4200)
db.add_message(main, "user", "ok run it")
for i, (cmd, out, err) in enumerate(
    [
        (
            "docker compose -f /opt/pernix/docker-compose.yml build --no-cache pernix",
            "[+] Building 212.4s (14/14) FINISHED\n => [internal] load build definition from Dockerfile\n => => transferring dockerfile: 1.2kB\n"
            * 6,
            False,
        ),
        (
            "docker compose up -d --force-recreate pernix && docker logs --tail 40 pernix",
            "pernix  | INFO  uvicorn running on http://0.0.0.0:8000\npernix  | INFO  migration v33 applied\n" * 5,
            False,
        ),
        (
            "curl -fsS http://127.0.0.1:8000/api/health | jq .maintenance.snooze",
            "curl: (7) Failed to connect to 127.0.0.1 port 8000 after 0 ms: Connection refused",
            True,
        ),
    ]
):
    cid = f"call_m{i}"
    tc = json.dumps(
        [
            {
                "id": cid,
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": cmd, "timeout": 300})},
            }
        ]
    )
    db.add_message(
        main,
        "assistant",
        "",
        tool_calls=tc,
        latency_ms=900,
        metadata=json.dumps({"model": "qwen3-27b", "latency_ms": 900}),
    )
    db.add_message(
        main, "tool", out, tool_call_id=cid, metadata=json.dumps({"was_error": err, "latency_ms": 140 + i * 900})
    )
asst(
    main,
    "The container is back up but the health probe was refused for a second while uvicorn bound the port. Retrying in five seconds.",
    2100,
)
db.add_message(main, "user", "fine. also what's the model chip say now")
asst(
    main,
    "It shows the fallback model, because the primary timed out twice during the rebuild.",
    1500,
    model="llama-3.1-8b-fallback",
)

# --- a long session for paging ----------------------------------------------
long_sid = db.create_session(title="The long one — paging test")
for i in range(1, 121):
    if i % 2:
        db.add_message(long_sid, "user", f"Question number {i}. What happens at step {i}?")
    else:
        asst(
            long_sid,
            f"Answer number {i}. Step {i} runs the pipeline and writes its result to disk.",
            1200 + (i % 7) * 400,
        )

# --- a parent with workers ----------------------------------------------------
parent_sid = db.create_session(title="Fan-out parent — index the corpus")
db.add_message(parent_sid, "user", "Index the whole corpus, three workers.")
asst(parent_sid, "Spawning three workers.", 3100)
for name, st in (
    ("crawl the docs", "processing"),
    ("write the tests", "processing"),
    ("benchmark the index", "paused"),
):
    wid = db.create_session(title=name, session_type="worker", parent_session_id=parent_sid)
    db.update_session(wid, state_v2=st, state=st, worker_kind="research")
    db.add_message(wid, "user", f"Do: {name}")
    asst(wid, f"Working on: {name}.", 1500)

# --- ordinary sessions + a space ----------------------------------------------
try:
    space_id = db.create_space(label="Pernix", color="#c9a227", slug="pernix")["id"]
except Exception as e:  # noqa: BLE001
    print("space skipped:", e, file=sys.stderr)
    space_id = None
for t in (
    "Rewrite the deploy script so it survives a reboot of the box",
    "Cron: nightly sweep",
    "Notes on the audit",
    "Snooze: curiosity drive",
    "Short one",
):
    # "normal", not "chat": 'chat' is the legend's word for this type and
    # 'normal' is the column's, and POST /api/sessions coerces anything else
    # away — so a seeded 'chat' was a session_type no real instance can hold,
    # and it showed up as a type of its own the moment the list endpoint
    # started reporting type_counts.
    sid = db.create_session(
        title=t,
        space_id=space_id if t.startswith("Rewrite") else None,
        session_type="cron" if t.startswith("Cron") else ("snooze" if t.startswith("Snooze") else "normal"),
    )
    db.add_message(sid, "user", f"about {t}")
    time.sleep(0.005)

# --- a second space, whose label is longer than the header is wide ------------
# The space header's own truncation case. Created last so it sorts after
# "Pernix" (create_space takes MAX(sort_order) + 1), which leaves every box the
# desktop baseline records where it was: the first .session-item in the DOM
# still belongs to the Pernix group above it.
if space_id:
    try:
        lab_id = db.create_space(
            label="Research lab — long-running literature review and notes",
            color="#4f9d8c",
            slug="research-lab",
        )["id"]
        lab_sid = db.create_session(title="Survey the retrieval papers", space_id=lab_id)
        db.add_message(lab_sid, "user", "Start with the 2024 survey and work backwards.")
    except Exception as e:  # noqa: BLE001
        print("long-label space skipped:", e, file=sys.stderr)

# --- a third space, deep enough to need shape --------------------------------
# Twenty sessions across every time bucket. The first two spaces hold one
# session each and so render flat, exactly as they always did — which is what
# keeps the desktop baseline's first .session-item inside the Pernix group at
# the top. This one is created last, so it sorts last, and it is the only
# place bucket headers and "Show all 20" appear.
#
# updated_at is written straight into the throwaway's sqlite: create_session
# and add_message both stamp it with now, and the ages are the whole point.
SPREAD = [("Today", 0.0), ("Yesterday", 1.0), ("This Week", 3.0), ("This Month", 15.0), ("Older", 60.0)]


def _age(sid, days):
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (when, sid))


scale_id = None
if space_id:
    try:
        scale_id = db.create_space(label="Scale", color="#b06ab3", slug="scale")["id"]
        for bucket, days in SPREAD:
            for i in range(4):
                sid = db.create_session(title=f"{bucket} thread {i + 1}", space_id=scale_id)
                db.add_message(sid, "user", f"A session that last moved {days:g} days ago.")
                _age(sid, days + i * 0.01)
    except Exception as e:  # noqa: BLE001
        print("scale space skipped:", e, file=sys.stderr)

# --- two pending space suggestions -------------------------------------------
# Seeded straight through the db helper the scan itself calls, so the gate
# exercises the client against exactly the rows a real scan writes without
# needing a model. Both kinds are here because they render differently, open
# different sheets and end in different buttons.
#
# The members are LOOSE sessions (no space_id): a suggestion is an offer to
# file chats that are not filed. Their created_at is spread over five calendar
# days — the real gate needs 3+ distinct days before it will offer a group at
# all, and a fixture that could not have survived that gate would be lying.
# updated_at is left at `now`, so they sit in Today and the accepted space
# renders them all without a folded bucket.
FACT_TITLES = [
    "Check the claim about the 2019 outage against the incident log",
    "Is the 40% figure in the vendor deck actually from their own data?",
    "Verify the three citations in the draft post",
    "Which of these two conflicting changelogs is the real one?",
    "Trace the quote back to whoever first wrote it",
]
MOVE_TITLES = [
    "Why did the box restart itself on Tuesday?",
    "Trim the image so the build stops timing out",
    "Read the compose file and tell me what binds to 8000",
]


def _age_created(sid, days):
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET created_at = ? WHERE id = ?", (when, sid))


fact_ids = []
for i, t in enumerate(FACT_TITLES):
    sid = db.create_session(title=t)
    db.add_message(sid, "user", f"about {t}")
    _age_created(sid, 4 - i)
    fact_ids.append(sid)

move_ids = []
for i, t in enumerate(MOVE_TITLES):
    sid = db.create_session(title=t)
    db.add_message(sid, "user", f"about {t}")
    _age_created(sid, 5 - i)
    move_ids.append(sid)

if space_id:
    try:
        # The move first, so the newest row — the one the list endpoint puts
        # at the top — is the "Fact checking" one the accept path drives.
        db.add_space_suggestion(
            "existing",
            "pernix-deploys",
            "Pernix deploys",
            "#c9a227",
            "Three loose chats are about the same box the Pernix space already tracks.",
            move_ids,
            existing_space_id=space_id,
        )
        time.sleep(0.01)
        db.add_space_suggestion(
            "new",
            "fact-checking",
            "Fact checking",
            "#4db6ac",
            "Five chats over five days all check a claim against a source.",
            fact_ids,
            directives={
                "RULES": {
                    "addition": "## Fact checking\n- Separate the claim, the evidence and the verdict.",
                    "rationale": "Fact checks need a fixed shape.",
                }
            },
        )
    except Exception as e:  # noqa: BLE001
        print("space suggestions skipped:", e, file=sys.stderr)

# --- canary runs, enough of them to crowd a page -----------------------------
# The composition problem in miniature: on the owner's box 277 of the 500
# newest sessions are self-checks. Every one of these is OLDER than the rows
# the desktop baseline measures, so they land in buckets below the ones it
# records and move nothing above them.
for i in range(30):
    cid = db.create_session(title=f"Canary: file-create #{i + 1}", session_type="canary")
    _age(cid, 2.0 + i * 0.5)


# --- the State timeline's own session -----------------------------------------
# session_state_log is the one table the seed above never touched, so the
# timeline modal had nothing to draw: the Graph tab said "No state transitions
# yet" and the Lane tab would have had no turns. The colour check used to write
# its own arc straight into sqlite from check.py; it lives here now, beside
# every other shape the gate seeds, and it is a whole story rather than one
# turn's transitions — three turns with the messages `db.get_turns` parses back
# into a scout report, a reflect chain, an eval gate, a compaction and a notice
# (tests/test_session_turns.py is the reference for those shapes).
#
# Its OWN session, deliberately. The lane needs matching messages, and the
# desktop baseline pins the height of the main session's transcript — adding
# eleven rows to it would move .messages-inner and fail a check that is not
# about the timeline at all.
#
# Created last so it sorts after everything else. Space groups render above the
# time buckets, so the first .session-item in the DOM stays where the baseline
# recorded it, inside the Pernix group.
TIMELINE_TITLE = "State timeline — three turns"
TL_BASE = int(time.time() * 1000) - 45 * 60 * 1000  # 45 minutes ago
SEC = 1000


def _tl_iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def tl_msg(sid, role, ms, content="", **kw):
    """Insert a message on the fixture's clock. add_message stamps `now`, and
    /turns joins messages to turns by time window — so an un-restamped row
    lands in whichever turn happens to be open at seed time, i.e. none."""
    mid = db.add_message(sid, role, content, **kw)
    with connect_sessions() as conn:
        conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (_tl_iso(ms), mid))
    return mid


def tl_usage(sid, ms, prompt, completion, model="qwen3-27b", cost=None):
    db.add_token_usage(
        sid,
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_estimate=cost,
    )
    # token_usage.created_at is SQLite's CURRENT_TIMESTAMP shape (naive UTC,
    # second resolution), not the ISO stamp messages carry.
    stamp = datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with connect_sessions() as conn:
        row = conn.execute("SELECT MAX(id) AS m FROM token_usage WHERE session_id = ?", (sid,)).fetchone()
        conn.execute("UPDATE token_usage SET created_at = ? WHERE id = ?", (stamp, row["m"]))


def tl_round(sid, ms, calls, results):
    """One agent round: the assistant row carrying the calls (its timestamp is
    where the lane draws the ticks), then a tool row per result.
    calls = [(id, name, args)]; results = [(id, content, latency_ms, error)]."""
    tl_msg(
        sid,
        "assistant",
        ms,
        "",
        tool_calls=json.dumps([{"id": cid, "name": name, "arguments": json.dumps(args)} for cid, name, args in calls]),
        latency_ms=900,
        metadata=json.dumps({"model": "qwen3-27b", "latency_ms": 900}),
    )
    for cid, content, latency, err in results:
        tl_msg(
            sid,
            "tool",
            ms + 1,
            content,
            tool_call_id=cid,
            latency_ms=latency,
            metadata=json.dumps({"was_error": err, "latency_ms": latency}),
        )


def tl_log(sid, turn, frm, to, reason, ms, **kw):
    db.add_state_log(sid, turn_id=turn, from_state=frm, to_state=to, reason=reason, timestamp_ms=ms, **kw)


tl_sid = db.create_session(title=TIMELINE_TITLE)

# ---- turn 1: one reflect retry. Phases 10/30/5/10/40/5 of 100s.
t1 = TL_BASE
tl_msg(tl_sid, "user", t1 - 2, "Reconcile the ledger and tell me what drifted.")
tl_log(tl_sid, 1, "idle_ready", "scouting", "prompt-arrived", t1, elapsed_ms=71_000)
tl_msg(
    tl_sid,
    "scout",
    t1 + 2 * SEC,
    json.dumps(
        {
            "type": "scout.done",
            "approach": "Read the ledger, then reconcile it account by account.",
            "tools": ["file_read", "bash"],
            "tool_rationale": "One read for the ledger, one shell call for the reconcile script.",
            "memory": "The tolerance was raised to 0.005 in March.",
            "model": "qwen3-27b",
            "scout_model": "qwen3-8b",
            "latency_ms": 1900,
            "from_cache": False,
            "from_fallback": False,
            "reused_prior": False,
        }
    ),
)
tl_log(tl_sid, 1, "scouting", "processing", "scout-done", t1 + 10 * SEC, elapsed_ms=10 * SEC)
tl_round(
    tl_sid,
    t1 + 18 * SEC,
    [("tl_a", "file_read", {"path": "ledger.json"}), ("tl_b", "bash", {"command": "python reconcile.py"})],
    [("tl_a", '{"accounts": 412}', 38, False), ("tl_b", "Error: reconcile.py: no such file", 12, True)],
)
tl_usage(tl_sid, t1 + 20 * SEC, 1800, 240)
tl_log(
    tl_sid,
    1,
    "processing",
    "finalizing",
    "loop-complete",
    t1 + 40 * SEC,
    elapsed_ms=30 * SEC,
    termination_reason="complete",
)
tl_msg(
    tl_sid,
    "reflect",
    t1 + 42 * SEC,
    json.dumps(
        {
            "verdict": "retry",
            "reasoning": "The reconcile script was never found and the failure was not investigated.",
            "diagnostic": "Gave up on the missing file instead of looking for it.",
            "what_worked": "The ledger read.",
        }
    ),
)
tl_log(
    tl_sid,
    1,
    "finalizing",
    "scouting",
    "reflect-retry",
    t1 + 45 * SEC,
    elapsed_ms=5 * SEC,
    retry_index=1,
    reflect_count=1,
)
tl_msg(
    tl_sid,
    "scout",
    t1 + 47 * SEC,
    json.dumps({"type": "scout.done", "approach": "Find the script first.", "tools": ["bash"], "reused_prior": True}),
)
tl_log(
    tl_sid,
    1,
    "scouting",
    "processing",
    "scout-done",
    t1 + 55 * SEC,
    elapsed_ms=10 * SEC,
    retry_index=1,
    reflect_count=1,
)
tl_round(
    tl_sid,
    t1 + 60 * SEC,
    [("tl_c", "bash", {"command": "find . -name reconcile.py"})],
    [("tl_c", "./tools/reconcile.py", 24, False)],
)
tl_usage(tl_sid, t1 + 62 * SEC, 2400, 310)
tl_log(
    tl_sid,
    1,
    "processing",
    "finalizing",
    "loop-complete",
    t1 + 95 * SEC,
    elapsed_ms=40 * SEC,
    retry_index=1,
    reflect_count=1,
    termination_reason="complete",
)
tl_msg(
    tl_sid,
    "reflect",
    t1 + 97 * SEC,
    json.dumps({"verdict": "pass", "reasoning": "Found it and ran it.", "diagnostic": "", "what_worked": "The retry."}),
)
tl_log(
    tl_sid,
    1,
    "finalizing",
    "idle_ready",
    "turn-complete",
    t1 + 100 * SEC,
    elapsed_ms=5 * SEC,
    retry_index=1,
    reflect_count=1,
)

# ---- turn 2: a compaction round trip. Phases 10/25/20/40/5 of 120s.
t2 = TL_BASE + 300 * SEC
tl_msg(tl_sid, "user", t2 - 2, "Now do the same for the archive.")
tl_log(tl_sid, 2, "idle_ready", "scouting", "prompt-arrived", t2, elapsed_ms=200 * SEC)
tl_msg(
    tl_sid,
    "scout",
    t2 + 2 * SEC,
    json.dumps(
        {
            "type": "scout.done",
            "approach": "Walk the archive year by year rather than loading it whole.",
            "tools": ["bash"],
            "tool_rationale": "The archive is too large to read inline.",
            "model": "qwen3-27b",
            "scout_model": "qwen3-8b",
            "latency_ms": 2400,
            "from_cache": True,
        }
    ),
)
tl_log(tl_sid, 2, "scouting", "processing", "scout-done", t2 + 12 * SEC, elapsed_ms=12 * SEC)
tl_round(
    tl_sid,
    t2 + 20 * SEC,
    [("tl_d", "bash", {"command": "ls archive/"})],
    [("tl_d", "2019\n2020\n2021\n2022\n2023", 31, False)],
)
tl_usage(tl_sid, t2 + 22 * SEC, 5200, 420, cost=0.0182)
tl_log(
    tl_sid, 2, "processing", "compacting", "compact-proactive", t2 + 42 * SEC, elapsed_ms=30 * SEC, compaction_count=1
)
tl_msg(
    tl_sid,
    "compaction",
    t2 + 50 * SEC,
    '```json\n{"goal": "Reconcile the archive", "progress": ["listed five years"],'
    ' "next": "reconcile 2019"}\n```\n\nA prose recap the model adds after the fence.',
    metadata=json.dumps({"compacted_up_to": 42, "original_count": 190}),
)
tl_log(tl_sid, 2, "compacting", "processing", "compact-done", t2 + 66 * SEC, elapsed_ms=24 * SEC, compaction_count=1)
tl_usage(tl_sid, t2 + 70 * SEC, 900, 60, cost=0.0031)
tl_log(
    tl_sid,
    2,
    "processing",
    "finalizing",
    "loop-complete",
    t2 + 114 * SEC,
    elapsed_ms=48 * SEC,
    compaction_count=1,
    termination_reason="complete",
)
tl_msg(tl_sid, "notice", t2 + 116 * SEC, "💭 [contradiction] 2021 reconciles twice with different totals")
tl_log(tl_sid, 2, "finalizing", "idle_ready", "turn-complete", t2 + 120 * SEC, elapsed_ms=6 * SEC, compaction_count=1)

# ---- turn 3, the newest: a plain turn with a verdict and a gate.
# Phases 10/70/20 of 80s. Its story is what the gate's Story checks read.
t3 = TL_BASE + 600 * SEC
tl_msg(tl_sid, "user", t3 - 2, "Run the tests before you call it done.")
tl_log(tl_sid, 3, "idle_ready", "scouting", "prompt-arrived", t3, elapsed_ms=180 * SEC)
tl_msg(
    tl_sid,
    "scout",
    t3 + 2 * SEC,
    json.dumps(
        {
            "type": "scout.done",
            "approach": "Run the suite, read the one failure, fix it and re-run.",
            "tools": ["bash", "file_read", "file_write"],
            "tool_rationale": "The gate is a shell command; the fix is a two-line edit.",
            "memory": "",
            "model": "qwen3-27b",
            "scout_model": "qwen3-8b",
            "latency_ms": 2100,
            "from_cache": False,
            "from_fallback": True,
            "reused_prior": False,
        }
    ),
)
tl_log(tl_sid, 3, "scouting", "processing", "scout-done", t3 + 8 * SEC, elapsed_ms=8 * SEC)
tl_round(
    tl_sid,
    t3 + 16 * SEC,
    [("tl_e", "bash", {"command": "pytest -q"}), ("tl_f", "file_read", {"path": "tools/reconcile.py"})],
    [("tl_e", "1 failed, 212 passed", 4100, True), ("tl_f", "def reconcile(...):", 18, False)],
)
tl_round(
    tl_sid,
    t3 + 40 * SEC,
    [("tl_g", "file_write", {"path": "tools/reconcile.py", "content": "…"})],
    [("tl_g", "written", 22, False)],
)
tl_usage(tl_sid, t3 + 44 * SEC, 620, 180)
tl_log(
    tl_sid,
    3,
    "processing",
    "finalizing",
    "loop-complete",
    t3 + 64 * SEC,
    elapsed_ms=56 * SEC,
    reflect_count=1,
    eval_count=1,
    termination_reason="complete",
)
tl_msg(
    tl_sid,
    "eval",
    t3 + 66 * SEC,
    json.dumps(
        {
            "kind": "gate",
            "attempt": 1,
            "gates": [
                {
                    "kind": "gate",
                    "name": "tests",
                    "command": "pytest -q tools/",
                    "passed": True,
                    "exit_code": 0,
                    "output_tail": "213 passed in 4.10s",
                    "reused": False,
                    "error": "",
                }
            ],
        }
    ),
)
tl_msg(
    tl_sid,
    "reflect",
    t3 + 70 * SEC,
    json.dumps(
        {
            "verdict": "pass",
            "reasoning": "The suite is green and the fix is the one the failure asked for.",
            "diagnostic": "",
            "what_worked": "Reading the failure before editing.",
        }
    ),
)
tl_log(
    tl_sid,
    3,
    "finalizing",
    "idle_ready",
    "turn-complete",
    t3 + 80 * SEC,
    elapsed_ms=16 * SEC,
    reflect_count=1,
    eval_count=1,
)

print(json.dumps({"main": main, "long": long_sid, "parent": parent_sid, "scale": scale_id, "timeline": tl_sid}))
