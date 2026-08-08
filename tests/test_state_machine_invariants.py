"""Invariant tests for the v2 state machine.

These tests lock down structural properties of the graph and assert that
callers of the state machine do the right things at the right places.
They're cheap to run and catch drift — e.g. if someone re-introduces
force_state() or bypasses transition().
"""

from __future__ import annotations

import re
from pathlib import Path

from sessions import state_v2 as sv2

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Graph shape
# ---------------------------------------------------------------------------


def test_every_state_has_at_least_one_incoming_edge():
    """Any state that's declared should be reachable from somewhere.
    Otherwise it's dead."""
    incoming = {target for (_src, _r), target in sv2.TRANSITIONS.items()}
    for state in sv2.SessionStateV2:
        if state is sv2.SessionStateV2.IDLE_READY:
            continue  # initial state — always entered at session creation
        assert state in incoming, f"{state.value} has no incoming edges"


def test_every_state_has_at_least_one_outgoing_edge():
    """Terminal states would have none, but v2 has no terminals (sessions
    just cycle IDLE_READY ↔ active). Flag if any state is a dead-end."""
    outgoing = {src for (src, _r), _t in sv2.TRANSITIONS.items()}
    for state in sv2.SessionStateV2:
        assert state in outgoing, f"{state.value} has no outgoing edges (dead end)"


def test_no_transition_targets_a_state_not_in_enum():
    states = set(sv2.SessionStateV2)
    for (src, reason), target in sv2.TRANSITIONS.items():
        assert src in states, f"bad source {src!r}"
        assert target in states, f"bad target {target!r}"


def test_every_reason_in_transitions_is_in_migration_check_constraint():
    """Migration v13 CHECK constraint on reason must list every reason the
    graph uses (plus the reaper/invariant suffixes). If we add a new reason
    to the graph, we have to update the migration and vice versa — this
    test catches the mismatch."""
    # Extract reasons from the migration DDL.
    db_file = REPO_ROOT / "db" / "database.py"
    src = db_file.read_text()
    match = re.search(
        r"reason TEXT NOT NULL CHECK \(reason IN \((.*?)\)\)",
        src,
        re.DOTALL,
    )
    # Temporary: we chose NOT to include the CHECK constraint in the final
    # migration to avoid blocking invariant-violation writes. If the migration
    # ever reintroduces CHECK, this test will flag that its list must match
    # the graph + {'invariant-violation:*', 'reaper-unstick', ...}.
    if match is None:
        return  # CHECK was omitted — skip the comparison
    migration_reasons = set(re.findall(r"'([^']+)'", match.group(1)))
    graph_reasons = {reason for (_s, reason) in sv2.TRANSITIONS.keys()}
    missing = graph_reasons - migration_reasons
    assert not missing, f"reasons in graph but not in migration CHECK: {missing}"


# ---------------------------------------------------------------------------
# No production caller bypasses the mutator
# ---------------------------------------------------------------------------


def _production_sources():
    """Yield (relative path, source) for every non-test production module."""
    for path in REPO_ROOT.glob("**/*.py"):
        if "tests" in path.parts:
            continue
        if "/.venv/" in str(path) or "/venv/" in str(path):
            continue
        try:
            yield str(path.relative_to(REPO_ROOT)), path.read_text()
        except Exception:
            continue


def test_no_production_callers_import_force_state():
    """The force-state escape hatch is gone entirely — `force_state()` and
    its successor `_force_state_for_tests()` were both deleted. Nothing may
    reintroduce one: production must go through
    sessions.state_v2.transition()."""
    offenders = []
    # Match both the current and legacy names so a revert would still fail.
    patterns = ("_force_state_for_tests(", "force_state(")
    for rel, src in _production_sources():
        if any(p in src for p in patterns):
            offenders.append(rel)
    assert not offenders, (
        f"force-state API called from production code: {offenders}. "
        "Every mutation must go through sessions.state_v2.transition() — "
        "add a new edge to the graph if you need to 'force' something."
    )


def test_no_production_callers_write_retired_flags():
    """post_hooks_complete and waiting_for_input are now read-only properties
    derived from the v2 state. Writes to them would raise AttributeError at
    runtime; this test catches them before the tests even load."""
    pattern = re.compile(r"\.(?:post_hooks_complete|waiting_for_input)\s*=")
    offenders = []
    for rel, src in _production_sources():
        for i, line in enumerate(src.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        "Writes to retired flags found:\n  "
        + "\n  ".join(offenders)
        + "\nUse sessions.state_v2.transition() instead — flags are properties now."
    )


# Symbols retired when the v1→v2 migration finished. Each pattern matches
# *use* (attribute access, call, import), not prose — the docstrings in
# sessions/state.py and sessions/state_v2.py still name the old enum when
# explaining what was removed, and should stay readable.
# The negative lookahead keeps SessionStateV2 out of the SessionState match.
_RETIRED_SYMBOLS = (
    (r"\bSessionState(?!V2)\s*[.(\[]", "the legacy SessionState enum"),
    (r"import\s+[^\n]*\bSessionState(?!V2)\b", "an import of the legacy SessionState enum"),
    (r"\bset_session_state\s*\(", "db.set_session_state (the legacy-column writer)"),
    (r"\b_LEGACY_TO_V2\b|\b_V2_TO_LEGACY\b", "a legacy/v2 bridge table"),
    (r"\b_persist_legacy_state\b", "the legacy-state persist helper"),
    (r"session[_\w]*\.state\s*=", "a direct write to session.state"),
)


def test_no_production_module_references_the_retired_legacy_layer():
    """The v1 state layer is deleted, not deprecated.

    Reintroducing any of it — the 5-value enum, a `session.state` mirror,
    the bridge tables, or the per-transition UPDATE of the legacy
    `sessions.state` column — would restore the exact bug class v2 was
    built to end: two sources of truth that disagree, one of which
    collapses five distinct states into "idle". Everything goes through
    sessions.state_v2.transition()."""
    offenders = []
    for rel, src in _production_sources():
        for pattern, what in _RETIRED_SYMBOLS:
            rx = re.compile(pattern)
            for i, line in enumerate(src.splitlines(), 1):
                if rx.search(line):
                    offenders.append(f"{rel}:{i}: {what} — {line.strip()}")
    assert not offenders, "Retired legacy state layer referenced in production code:\n  " + "\n  ".join(offenders)


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


def test_compat_status_covers_every_state():
    for state in sv2.SessionStateV2:
        assert state in sv2.COMPAT_STATUS, f"{state} missing from COMPAT_STATUS"
