#!/usr/bin/env python3
"""Seed the mobile-audit app with realistic transcript shapes.
Run with cwd = the smoke appdir.  seed.py <repo>
"""

import json
import sys
import time

sys.path.insert(0, sys.argv[1])
from db import models as db  # noqa: E402

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
    sid = db.create_session(
        title=t,
        space_id=space_id if t.startswith("Rewrite") else None,
        session_type="cron" if t.startswith("Cron") else ("snooze" if t.startswith("Snooze") else "chat"),
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

print(json.dumps({"main": main, "long": long_sid, "parent": parent_sid}))
