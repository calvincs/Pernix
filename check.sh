#!/usr/bin/env bash
# check.sh — format, lint, and test the Pernix codebase.
#
# Usage:
#   ./check.sh          # check everything; fix nothing
#   ./check.sh --fix    # apply black + ruff auto-fixes, then check
#
# Exit 0 only when all checks and tests pass with ≥60% coverage.

set -euo pipefail

VENV_PYTHON=".venv/bin/python3.12"
VENV_BIN=".venv/bin"

RESULTS=()
OVERALL=0

# ── helpers ──────────────────────────────────────────────────────────────────

step() { echo; echo "══════════════════════════════════════════"; echo "  $*"; echo "══════════════════════════════════════════"; }
ok()   { echo "  ✓ $*"; RESULTS+=("PASS  $*"); }
fail() { echo "  ✗ $*"; RESULTS+=("FAIL  $*"); OVERALL=1; }

require_venv() {
    if [[ ! -x "$VENV_PYTHON" ]]; then
        echo "ERROR: virtual environment not found at .venv/"
        echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt"
        exit 1
    fi
}

# ── main ─────────────────────────────────────────────────────────────────────

cd "$(dirname "$0")"
require_venv

FIX=0
for arg in "$@"; do
    [[ "$arg" == "--fix" ]] && FIX=1
done

# ── 1. black ─────────────────────────────────────────────────────────────────
step "black — code formatting"
if [[ $FIX -eq 1 ]]; then
    "$VENV_BIN/black" . && ok "black (formatted in-place)"
else
    if "$VENV_BIN/black" --check --diff . 2>&1; then
        ok "black"
    else
        fail "black — run './check.sh --fix' to auto-format"
    fi
fi

# ── 2. ruff ──────────────────────────────────────────────────────────────────
step "ruff — fast linter"
if [[ $FIX -eq 1 ]]; then
    "$VENV_BIN/ruff" check . --fix 2>&1 || true
fi
if "$VENV_BIN/ruff" check . 2>&1; then
    ok "ruff"
else
    fail "ruff"
fi

# ── 3. flake8 ────────────────────────────────────────────────────────────────
step "flake8 — style checks"
if "$VENV_BIN/flake8" . 2>&1; then
    ok "flake8"
else
    fail "flake8"
fi

# ── 4. pytest + coverage ─────────────────────────────────────────────────────
step "pytest — unit tests + coverage (≥63%)"
if "$VENV_PYTHON" -m pytest \
        --cov \
        --cov-report=term-missing \
        --cov-fail-under=63 \
        -n auto 2>&1; then
    ok "pytest (coverage ≥ 63%)"
else
    fail "pytest (tests failed or coverage < 63%)"
fi

# ── summary ──────────────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════"
echo "  SUMMARY"
echo "══════════════════════════════════════════"
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo

if [[ $OVERALL -eq 0 ]]; then
    echo "  ALL CHECKS PASSED"
else
    echo "  CHECKS FAILED — see above"
    exit 1
fi
