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

from core.canary.parser import HOLDOUT_TAG, CanaryDef, CanaryParseError, canaries_dir, load_canary, scan_canaries
from core.canary.runner import CanaryRunResult, run_canary, run_sweep

__all__ = [
    "HOLDOUT_TAG",
    "CanaryDef",
    "CanaryParseError",
    "CanaryRunResult",
    "canaries_dir",
    "load_canary",
    "prompt_safe_canaries",
    "run_canary",
    "run_sweep",
    "scan_canaries",
]


def prompt_safe_canaries(base=None) -> list[CanaryDef]:
    """Every canary EXCEPT the holdouts — the only list that may be quoted
    into a producer prompt (refine, dream) or offered to a producer as an
    edit target.

    Nothing renders canary names into a prompt today; this exists so that
    when something does, the safe list is the one already at hand. A test
    asserts the producer prompts stay free of holdout names.
    """
    return [c for c in scan_canaries(base) if not c.holdout]
