"""Pernix — Golden-task canary suite (adaptation plan §5).

Active measurement for self-improvement: canned tasks + deterministic gates,
run headlessly through the FULL pipeline (scout → agent → gates → reflect).
The Phase 4 tripwire's primary signal.

Canary sessions are session_type="canary" and isolated by an enumerated
predicate list (FTS exclusion, distill/refine exclusion, candor early-return,
stamped post-mortems, memory writes denied, snooze transparency) — see the
plan for the full list. A canary run scores the FINAL reflect attempt's
gates; the retry count is recorded alongside.
"""

from core.canary.parser import CanaryDef, CanaryParseError, canaries_dir, load_canary, scan_canaries
from core.canary.runner import CanaryRunResult, run_canary, run_sweep

__all__ = [
    "CanaryDef",
    "CanaryParseError",
    "CanaryRunResult",
    "canaries_dir",
    "load_canary",
    "run_canary",
    "run_sweep",
    "scan_canaries",
]
