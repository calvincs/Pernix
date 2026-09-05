"""Generated fixture for the `gen-grep-count` canary (trust-loop W5).

Search-and-aggregate over seeded logs. The static `grep-count` canary has
one answer — 8 — sitting in its gate command in the repository; this one
picks a fresh file set, a fresh line mix and therefore a fresh count on
every run, while preserving both traps that made the original worth having:

  * a lowercase `error` line, which must NOT count (case-sensitive), and
  * an `ERRORS-SUMMARY` line, which MUST count (substring semantics).

Both traps are guaranteed present at least once per fixture, and the
expected count is derived from the rendered lines rather than tallied as
they are appended — the generator checks its own arithmetic against the
bytes the agent will actually read.
"""

from __future__ import annotations

import random

_SERVICES = ("app", "worker", "audit", "api", "scheduler", "gateway", "indexer")
_INFO = (
    "service started",
    "db connected",
    "worker pool up",
    "cache warm",
    "config reloaded",
    "audit trail enabled",
)
_WARN = ("retrying in 5s", "queue depth 40", "slow query 1.2s", "disk 81% full")
_ERROR = (
    "db connection refused",
    "timeout on /api/report",
    "job {n} failed: KeyError('user')",
    "permission denied for token rotation",
    "upstream 502 from billing",
)
_LOWER = (
    "error lowercase should not count",
    "recovered from a transient error, no alert raised",
    "handled error in the retry path",
)
_SUMMARY = (
    "ERRORS-SUMMARY generated",
    "nightly ERRORS-SUMMARY written to /var/reports",
)


def _stamp(rng: random.Random) -> str:
    return f"2026-08-{rng.randrange(1, 29):02d} {rng.randrange(0, 24):02d}:{rng.randrange(0, 60):02d}:{rng.randrange(0, 60):02d}"


def generate(seed: int) -> dict:
    rng = random.Random(seed)
    names = rng.sample(_SERVICES, rng.randrange(2, 5))  # 2..4 log files

    # Plan the line mix first so both traps are guaranteed somewhere in the
    # set, then shuffle each file's lines so their position moves too.
    per_file: dict[str, list[str]] = {}
    kinds: list[str] = []
    for _ in range(rng.randrange(3, 7) + len(names)):
        kinds.append(rng.choices(("info", "warn", "error", "lower"), weights=(3, 2, 4, 2))[0])
    kinds += ["error", "lower", "summary"]  # floors: the traps always exist
    if rng.random() < 0.4:
        kinds.append("summary")
    rng.shuffle(kinds)

    buckets: dict[str, list[str]] = {n: [] for n in names}
    for i, kind in enumerate(kinds):
        target = names[i % len(names)]
        if kind == "info":
            body = rng.choice(_INFO)
        elif kind == "warn":
            body = "WARN " + rng.choice(_WARN)
        elif kind == "error":
            body = "ERROR " + rng.choice(_ERROR).format(n=rng.randrange(100, 999))
        elif kind == "lower":
            body = rng.choice(_LOWER)
        else:
            body = "INFO " + rng.choice(_SUMMARY)
        if kind == "info":
            body = "INFO " + body
        buckets[target].append(f"{_stamp(rng)} {body}")

    for name in names:
        rng.shuffle(buckets[name])
        per_file[f"logs/{name}.log"] = buckets[name]

    files = {path: "\n".join(lines) + "\n" for path, lines in per_file.items()}

    # Ground truth read back off the rendered fixture, not off the plan.
    expected = sum(1 for content in files.values() for line in content.splitlines() if "ERROR" in line)

    prompt = (
        "The logs/ directory in the workspace contains application log files. Count\n"
        'how many lines across ALL files in logs/ contain the substring "ERROR"\n'
        "(case-sensitive; any occurrence anywhere in the line counts). Write just\n"
        "that number as a single line to answer.txt in the workspace root.\n"
    )

    return {
        "prompt": prompt,
        "files": files,
        "gates": [
            {
                "name": "answer_correct",
                "command": f"grep -qx '{expected}' answer.txt",
                "watch_paths": ["answer.txt"],
            }
        ],
    }
