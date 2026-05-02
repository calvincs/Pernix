#!/usr/bin/env python3
"""Pernix metrics CLI — snapshot of the feedback loop's state.

Usage:
    python scripts/metrics.py                # last 7 days
    python scripts/metrics.py --days 30      # last 30 days
    python scripts/metrics.py --json         # machine-readable output
    python scripts/metrics.py --since 2026-01-01 --until 2026-02-01

Read-only over the existing DB — no LLM calls, no writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when invoked directly.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import metrics  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pernix metrics reporter")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (ignored if --since given)")
    parser.add_argument("--since", type=str, default=None, help="Window start (ISO8601)")
    parser.add_argument("--until", type=str, default=None, help="Window end (ISO8601)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of plaintext")
    args = parser.parse_args(argv)

    report = metrics.compute(
        since_iso=args.since,
        until_iso=args.until,
        days=args.days,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(metrics.format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
