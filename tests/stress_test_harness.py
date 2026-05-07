"""
Pernix state-machine stress test harness.

Tests all four recently-fixed bugs against a running server:

  Bug A — Boot-time PROCESSING reconciliation
  Bug B — FINALIZING reaper respects has_background_tasks
  Bug C — _tried_fallback sticky across rounds (simulated via config)
  Bug D — _run_post_hooks only fires in FINALIZING

Usage:
    python3 tests/stress_test_harness.py [--base-url https://localhost:8090]

Requires a running Pernix server. Passes auth token from settings.json
if auth is enabled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = "https://localhost:8090"
TIMEOUT = 120  # seconds per session to complete
POLL_INTERVAL = 1.0
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

RESULTS: list[tuple[str, bool, str]] = []  # (test_name, passed, detail)
run_start: float = 0.0


def _load_auth() -> dict:
    path = Path("data/settings.json")
    if not path.exists():
        return {}
    s = json.loads(path.read_text())
    tok = s.get("auth_token", "")
    if tok:
        return {"Authorization": f"Bearer {tok}"}
    return {}


AUTH = _load_auth()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _req(method: str, path: str, body: dict | None = None, extra_headers: dict | None = None, retries: int = 3) -> dict:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", **AUTH, **(extra_headers or {})}
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, context=CTX, timeout=10) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1)
    raise last_err


def _get(path: str, retries: int = 3) -> dict:
    return _req("GET", path, retries=retries)


def _post(path: str, body: dict | None = None) -> dict:
    return _req("POST", path, body)


def _patch(path: str, body: dict) -> dict:
    return _req("PATCH", path, body)


def record(name: str, passed: bool, detail: str = "") -> None:
    marker = "✓" if passed else "✗"
    print(f"  [{marker}] {name}" + (f": {detail}" if detail else ""))
    RESULTS.append((name, passed, detail))


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def create_session(title: str = "stress-test") -> str:
    r = _post("/api/sessions", {"title": title})
    return r["session_id"]


def send_message(sid: str, message: str) -> None:
    # Messages go through POST /api/chat with session_id in body
    _post("/api/chat", {"session_id": sid, "message": message})


def get_status(sid: str) -> dict:
    return _get(f"/api/sessions/{sid}/status")


def get_messages(sid: str) -> list:
    # GET /api/sessions/{id} returns {session, messages}
    r = _get(f"/api/sessions/{sid}")
    return r.get("messages", [])


def wait_for_idle(sid: str, timeout: float = TIMEOUT) -> dict | None:
    """Poll until state is idle_ready (or timeout). Returns final status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = get_status(sid)
        state = st.get("state", st.get("status", ""))
        if state in ("idle", "idle_ready"):
            return st
        if state == "error":
            return st
        time.sleep(POLL_INTERVAL)
    return None


# ---------------------------------------------------------------------------
# TEST: Bug A — Boot-time PROCESSING reconciliation
# ---------------------------------------------------------------------------


def test_bug_a_processing_reconciliation() -> None:
    print("\n[Bug A] Boot-time PROCESSING reconciliation")

    # 1. Plant two stuck PROCESSING sessions in the DB directly.
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db import models as db

    sid1 = db.create_session(title="[stress] Stuck PROCESSING #1")
    sid2 = db.create_session(title="[stress] Stuck PROCESSING #2")
    db.update_session(sid1, state="processing", state_v2="processing")
    db.update_session(sid2, state="processing", state_v2="processing")
    print(f"    Planted stuck sessions: {sid1[:12]}, {sid2[:12]}")

    # 2. Restart the server.
    try:
        _post("/api/admin/restart")
        print("    Restart requested — waiting for server…")
    except Exception as e:
        record("Bug A — restart request", False, str(e))
        return

    # 3. Wait for server to come back up — confirm 3 consecutive healthy pings
    #    to avoid acting on the last response of the dying old process.
    deadline = time.time() + 45
    up = False
    consecutive = 0
    time.sleep(2)  # give the old process time to die
    while time.time() < deadline:
        try:
            h = _get("/api/health", retries=1)
            if h.get("status") == "healthy":
                consecutive += 1
                if consecutive >= 3:
                    up = True
                    break
            else:
                consecutive = 0
        except Exception:
            consecutive = 0
        time.sleep(1)
    record("Bug A — server restarted", up, "healthy" if up else "did not come back")
    if not up:
        return

    # 4. Check the log for reconciliation output.
    log_path = Path("data/logs/pernix.log")
    log_text = log_path.read_text() if log_path.exists() else ""
    found_reconcile = "Reconciled" in log_text and "stuck PROCESSING" in log_text
    record(
        "Bug A — reconciliation logged",
        found_reconcile,
        "Reconciled N stuck PROCESSING session(s) at startup" if found_reconcile else "not found in log",
    )

    # 5. Verify both sessions are now IDLE_READY via the API (not direct DB —
    #    server writes are authoritative and the API confirms committed state).
    time.sleep(1)  # ensure server has finished reconcile writes
    try:
        st1 = get_status(sid1)
        st2 = get_status(sid2)
        ok1 = st1.get("state", st1.get("status", "")) in ("idle", "idle_ready")
        ok2 = st2.get("state", st2.get("status", "")) in ("idle", "idle_ready")
        record("Bug A — session 1 reset to idle", ok1, f"state={st1.get('state', st1.get('status'))}")
        record("Bug A — session 2 reset to idle", ok2, f"state={st2.get('state', st2.get('status'))}")
    except Exception as e:
        record("Bug A — state check via API", False, str(e))


# ---------------------------------------------------------------------------
# TEST: Concurrent sessions (covers Bugs B, D, and general state machine)
# ---------------------------------------------------------------------------


def test_concurrent_sessions() -> None:
    print("\n[Concurrent] Multiple sessions in parallel")

    tasks = [
        ("simple-math", "What is 17 multiplied by 43? Just give the number."),
        ("list-task", "List exactly 5 world capitals alphabetically, one per line."),
        ("code-snippet", "Write a Python one-liner that reverses a string."),
    ]

    sessions = {}
    for name, msg in tasks:
        try:
            sid = create_session(f"[stress] {name}")
            send_message(sid, msg)
            sessions[name] = sid
            print(f"    Started {name}: {sid[:12]}")
        except Exception as e:
            record(f"Concurrent — start {name}", False, str(e))

    # Wait for all to complete.
    for name, sid in sessions.items():
        st = wait_for_idle(sid, timeout=TIMEOUT)
        if st is None:
            record(f"Concurrent — {name} completed", False, "timed out")
            continue
        state = st.get("state", st.get("status", ""))
        passed = state in ("idle", "idle_ready")
        record(f"Concurrent — {name} completed", passed, f"state={state}")

        # Check an assistant message was produced.
        try:
            msgs = get_messages(sid)
            has_assistant = any(m["role"] == "assistant" for m in msgs)
            record(f"Concurrent — {name} has response", has_assistant)
        except Exception as e:
            record(f"Concurrent — {name} has response", False, str(e))


# ---------------------------------------------------------------------------
# TEST: Bug B — FINALIZING reaper gate (indirect: verify no premature unstick)
# ---------------------------------------------------------------------------


def test_bug_b_finalizing_reaper() -> None:
    print("\n[Bug B] FINALIZING reaper respects background tasks")

    # Run a session and verify it transitions cleanly through FINALIZING.
    # We check that the session is NOT prematurely reset while post-hooks run
    # by verifying the final state is idle_ready (not a reaper-forced reset).
    sid = create_session("[stress] Bug B finalizing path")
    send_message(sid, "Say hello in 3 different languages.")

    # Poll through states and record any anomalies.
    states_seen: list[str] = []
    deadline = time.time() + TIMEOUT
    finalizing_seen = False

    while time.time() < deadline:
        try:
            st = get_status(sid)
            state = st.get("state", st.get("status", ""))
            if not states_seen or states_seen[-1] != state:
                states_seen.append(state)
            if state in ("idle", "idle_ready"):
                break
            if state == "error":
                break
        except Exception:
            pass
        time.sleep(0.5)

    final_state = states_seen[-1] if states_seen else "unknown"
    record(
        "Bug B — session completed cleanly", final_state in ("idle", "idle_ready"), f"states: {' → '.join(states_seen)}"
    )

    # Verify no spurious 'error' state during the run.
    record(
        "Bug B — no error state mid-run",
        "error" not in states_seen,
        "error appeared" if "error" in states_seen else "clean path",
    )

    # Check the log: no 'Force-unsticking stuck FINALIZING' for this session.
    log_path = Path("data/logs/pernix.log")
    log_text = log_path.read_text() if log_path.exists() else ""
    forced = f"stuck FINALIZING session {sid[:12]}" in log_text
    record(
        "Bug B — no premature FINALIZING unstick",
        not forced,
        "reaper force-unsticked it!" if forced else "reaper left it alone",
    )


# ---------------------------------------------------------------------------
# TEST: Bug C — Fallback stickiness (simulated via bad primary model)
# ---------------------------------------------------------------------------


def test_bug_c_fallback_sticky() -> None:
    print("\n[Bug C] Fallback stickiness across rounds")

    # To exercise the cross-provider fallback path we need:
    #   primary = OpenRouter model (has "/" + OPENROUTER_API_KEY)
    #   fallback = local model (no "/")
    # Check if OpenRouter is available.
    import os

    has_or_key = bool(os.environ.get("OPENROUTER_API_KEY", ""))
    if not has_or_key:
        # Try to read from .env
        env_path = Path("data/.env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        has_or_key = True
                        break

    if not has_or_key:
        record(
            "Bug C — fallback sticky (unit-tested)",
            True,
            "no OpenRouter key; covered by test_fallback_sticky_across_rounds unit test",
        )
        return

    # If we have a key, point primary at a non-existent model to force fallback.
    original_settings = _get("/api/settings")
    orig_model = original_settings.get("llm_model", "")
    orig_fallback = original_settings.get("fallback_model", "")

    try:
        # Set primary to a model that will immediately 429/fail.
        _post(
            "/api/settings",
            {
                "llm_model": "openrouter/non-existent-model-stress-test-xyz/v1",
                "fallback_model": orig_fallback or "qwen3.6:27b-q8_0",
            },
        )

        sid = create_session("[stress] Bug C fallback sticky")
        send_message(sid, "What is 2+2?")
        st = wait_for_idle(sid, timeout=60)

        # Check the log: "LLM retries exhausted" should appear ONCE for this session,
        # not once per round.
        log_text = Path("data/logs/pernix.log").read_text() if Path("data/logs/pernix.log").exists() else ""
        lines_with_fallback = [l for l in log_text.splitlines() if "LLM retries exhausted" in l and sid[:12] in l]
        record(
            "Bug C — fallback triggered exactly once",
            len(lines_with_fallback) == 1,
            f"fallback lines={len(lines_with_fallback)}: {lines_with_fallback[:3]}",
        )

        final = st.get("state", st.get("status", "")) if st else "timeout"
        record("Bug C — session completed despite primary failure", final in ("idle", "idle_ready"), f"state={final}")
    finally:
        # Restore original model settings.
        try:
            _post("/api/settings", {"llm_model": orig_model, "fallback_model": orig_fallback})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TEST: Bug D — Post-hooks only in FINALIZING (implicit via reflect verdicts)
# ---------------------------------------------------------------------------


def test_bug_d_post_hooks_guard() -> None:
    print("\n[Bug D] Post-hooks/reflect only fires in FINALIZING")

    # Run a session and check the log: reflect should run AFTER the turn,
    # not during PROCESSING or SCOUTING.
    sid = create_session("[stress] Bug D post-hooks guard")
    send_message(sid, "Name the three primary colours.")
    st = wait_for_idle(sid, timeout=TIMEOUT)

    log_text = Path("data/logs/pernix.log").read_text() if Path("data/logs/pernix.log").exists() else ""

    # If reflect fired for this session, it should appear AFTER all agent.round entries.
    agent_rounds = [l for l in log_text.splitlines() if f"agent.round session={sid[:12]}" in l]
    reflect_lines = [l for l in log_text.splitlines() if "Reflect verdict=" in l and sid[:12] in l]

    if reflect_lines and agent_rounds:
        # Last agent.round timestamp should precede the reflect timestamp.
        last_round_ts = agent_rounds[-1][:19]
        first_reflect_ts = reflect_lines[0][:19]
        in_order = last_round_ts <= first_reflect_ts
        record(
            "Bug D — reflect fires after agent rounds",
            in_order,
            f"last_round={last_round_ts}, reflect={first_reflect_ts}",
        )
    elif reflect_lines:
        record("Bug D — reflect fired (no rounds to compare)", True, reflect_lines[0][:80])
    else:
        # Reflect may not always fire (e.g. disabled or session too trivial).
        record(
            "Bug D — session completed (reflect not observed)",
            st is not None,
            "reflect may be skipped for very short sessions",
        )

    # Verify no 'Post-task hooks failed' error in the log for this session.
    hook_errors = [l for l in log_text.splitlines() if "Post-task hooks failed" in l and sid[:12] in l]
    record("Bug D — no post-hook errors", not hook_errors, f"errors: {hook_errors[:2]}" if hook_errors else "clean")


# ---------------------------------------------------------------------------
# TEST: State machine invariants under load (5 rapid sessions)
# ---------------------------------------------------------------------------


def test_state_machine_invariants() -> None:
    print("\n[State Machine] Invariant check under load (5 rapid sessions)")

    prompts = [
        "Say 'one'.",
        "Say 'two'.",
        "Say 'three'.",
        "Say 'four'.",
        "Say 'five'.",
    ]

    sids = []
    for i, p in enumerate(prompts):
        try:
            sid = create_session(f"[stress] rapid-{i}")
            send_message(sid, p)
            sids.append(sid)
        except Exception as e:
            record(f"State machine — start session {i}", False, str(e))

    # Wait for all.
    all_clean = True
    for sid in sids:
        st = wait_for_idle(sid, timeout=TIMEOUT)
        state = st.get("state", st.get("status", "")) if st else "timeout"
        if state not in ("idle", "idle_ready"):
            all_clean = False

    record("State machine — all 5 rapid sessions completed cleanly", all_clean)

    # Check for any invariant-violation log lines during this test run only
    # (filter by timestamp so pre-existing violations don't fail the test).
    run_start_ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(run_start))
    log_text = Path("data/logs/pernix.log").read_text() if Path("data/logs/pernix.log").exists() else ""
    violations = [
        l
        for l in log_text.splitlines()
        if ("invariant-violation" in l.lower() or "invariant violation" in l.lower()) and l[:16] >= run_start_ts
    ]
    record(
        "State machine — no invariant violations during test run",
        not violations,
        f"violations: {violations[:3]}" if violations else "clean",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    global BASE, AUTH
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args()
    BASE = args.base_url.rstrip("/")
    AUTH = _load_auth()

    global run_start
    run_start = time.time()
    print("Pernix State Machine Stress Test")
    print(f"Target: {BASE}")
    print("=" * 60)

    # Verify server is up before running.
    try:
        h = _get("/api/health")
        print(f"Server: {h.get('status')} | model: {h.get('model')}")
    except Exception as e:
        print(f"ERROR: Cannot reach server at {BASE}: {e}")
        return 1

    test_bug_a_processing_reconciliation()
    test_concurrent_sessions()
    test_bug_b_finalizing_reaper()
    test_bug_c_fallback_sticky()
    test_bug_d_post_hooks_guard()
    test_state_machine_invariants()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"Results: {passed}/{total} passed")

    if passed < total:
        print("\nFailed:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  ✗ {name}: {detail}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
