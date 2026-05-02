"""Benchmark for sessions.state_v2.transition() throughput and latency.

Target from the migration plan: <1ms per transition on warm SQLite WAL.
Not a pytest test — run manually:

    python -m tests.bench_state_transition
"""

from __future__ import annotations

import os
import statistics
import tempfile
import time
from pathlib import Path


def main(n: int = 1000) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pernix_bench_"))
    os.environ["CAI_BENCH"] = "1"
    from config import settings

    settings.db_path = str(tmp / "sessions.db")
    settings.workspace_dir = str(tmp / "workspace")
    settings.memory_dir = str(tmp / "memories")
    (tmp / "workspace").mkdir(exist_ok=True)
    (tmp / "memories").mkdir(exist_ok=True)

    from db import database, models

    database.init_db()

    from sessions import state_v2 as sv2
    from sessions.state import AgentSession

    sid = models.create_session(title="bench")
    session = AgentSession(session_id=sid)

    # Warm up: trigger WAL, compile the module, etc.
    for _ in range(50):
        sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
        sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
        sv2.transition(
            session,
            sv2.SessionStateV2.FINALIZING,
            "loop-complete",
            termination_reason=sv2.TerminationReason.COMPLETE,
        )
        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "turn-complete")

    # Measure: n full turns = 4*n transitions
    samples_ms: list[float] = []
    for _ in range(n):
        t = time.monotonic_ns()
        sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
        samples_ms.append((time.monotonic_ns() - t) / 1e6)

        t = time.monotonic_ns()
        sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
        samples_ms.append((time.monotonic_ns() - t) / 1e6)

        t = time.monotonic_ns()
        sv2.transition(
            session,
            sv2.SessionStateV2.FINALIZING,
            "loop-complete",
            termination_reason=sv2.TerminationReason.COMPLETE,
        )
        samples_ms.append((time.monotonic_ns() - t) / 1e6)

        t = time.monotonic_ns()
        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "turn-complete")
        samples_ms.append((time.monotonic_ns() - t) / 1e6)

    samples_ms.sort()
    mean = statistics.mean(samples_ms)
    median = statistics.median(samples_ms)
    p95 = samples_ms[int(len(samples_ms) * 0.95)]
    p99 = samples_ms[int(len(samples_ms) * 0.99)]
    mx = samples_ms[-1]

    rows = len(samples_ms)
    print(f"Samples: {rows} transitions ({n} turns × 4)")
    print(f"  mean   : {mean:8.3f} ms")
    print(f"  median : {median:8.3f} ms")
    print(f"  p95    : {p95:8.3f} ms")
    print(f"  p99    : {p99:8.3f} ms")
    print(f"  max    : {mx:8.3f} ms")
    print(f"Target: <1.0 ms mean; {'PASS' if mean < 1.0 else 'FAIL'}")
    # Show DB row count
    n_rows = len(models.get_state_log(sid, limit=100000))
    print(f"state_log rows persisted: {n_rows}")


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    main(n)
