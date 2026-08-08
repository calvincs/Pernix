"""Invariant tests for the SQLite connection layer.

`db/database.py` caches one connection per (thread, path) and relies on a
whole-repo invariant stated in a comment there:

    no code path nests connection contexts to the same DB in one thread

It matters because `with conn:` on a *shared* connection commits at the end of
the inner block — an inner `with connect_sessions()` inside an outer one
therefore commits the outer block's in-flight transaction, silently, with no
error and no rollback on a later failure. That was documented as "verified" and
enforced by nothing. These tests enforce it the way
`test_state_machine_invariants.py` enforces its own rules: by reading the
source tree.

APPROXIMATION — what these tests do and do not see
--------------------------------------------------
The check is static and intraprocedural. It flags:

  1. A `with connect_sessions()` / `with connect_memory()` block that
     lexically contains another `with` on the *same* connector.
  2. Inside `db/models.py`, a call from within a connection block to another
     module-level `db.models` function that itself opens one.
  3. Inside `core/`, `api/`, `sessions/`, a call of the form `db.foo(...)` /
     `models.foo(...)` from within a connection block, where `foo` is a
     `db.models` function that opens a connection.

It does NOT see nesting that crosses an indirection the reader cannot follow
either — a callback, a method dispatched off a stored object, a function
handed in as a parameter. That is a deliberate trade: a static approximation
that catches the shape people actually write beats no enforcement at all, and
anything it misses was already invisible. If it ever produces a false
positive, restructure the code rather than widening the allowlist — a
connection block that calls out to something opaque is exactly the pattern
this invariant exists to prevent.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories whose code runs against the cached connections.
SCANNED_DIRS = ("db", "core", "api", "sessions")

CONNECTORS = {"connect_sessions", "connect_memory"}


def _iter_sources() -> list[tuple[str, ast.Module]]:
    out: list[tuple[str, ast.Module]] = []
    for name in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / name).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text())
            except (OSError, SyntaxError):  # unreadable / not our Python
                continue
            out.append((str(path.relative_to(REPO_ROOT)), tree))
    return out


def _connector_of(node: ast.AST) -> str | None:
    """The connector a `with` statement opens, if any."""
    if not isinstance(node, (ast.With, ast.AsyncWith)):
        return None
    for item in node.items:
        call = item.context_expr
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name in CONNECTORS:
            return name
    return None


def _connection_blocks(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """(connector, node) for every `with connect_*()` in the module."""
    return [(c, n) for n in ast.walk(tree) for c in [_connector_of(n)] if c]


def _connecting_functions(tree: ast.Module) -> set[str]:
    """Module-level function names whose body opens a connection."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _connection_blocks(node):
            names.add(node.name)
    return names


def _location(rel: str, node: ast.AST) -> str:
    return f"{rel}:{getattr(node, 'lineno', '?')}"


# ---------------------------------------------------------------------------
# 1. No lexically nested connection contexts
# ---------------------------------------------------------------------------


def test_no_nested_connection_contexts():
    offenders = []
    for rel, tree in _iter_sources():
        for connector, block in _connection_blocks(tree):
            for child in block.body:
                for inner in ast.walk(child):
                    if _connector_of(inner) == connector:
                        offenders.append(
                            f"{_location(rel, inner)}: `with {connector}()` inside {_location(rel, block)}"
                        )
    assert not offenders, (
        "Nested connection contexts on the same database:\n  "
        + "\n  ".join(offenders)
        + "\nThe inner block's exit commits the outer block's in-flight "
        "transaction (they share one cached connection). Pass `conn` down "
        "instead of reopening."
    )


# ---------------------------------------------------------------------------
# 2. db/models.py does not call its own connection-opening helpers mid-block
# ---------------------------------------------------------------------------


def test_db_models_helpers_are_not_called_inside_a_connection_block():
    path = REPO_ROOT / "db" / "models.py"
    tree = ast.parse(path.read_text())
    opening = _connecting_functions(tree)
    offenders = []
    for connector, block in _connection_blocks(tree):
        for child in block.body:
            for node in ast.walk(child):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in opening:
                    offenders.append(f"{_location('db/models.py', node)}: {node.func.id}() inside `with {connector}()`")
    assert not offenders, (
        "db/models.py opens a connection inside another connection block:\n  "
        + "\n  ".join(offenders)
        + "\nThe callee reuses this thread's cached connection and commits on "
        "exit, ending the outer transaction early."
    )


# ---------------------------------------------------------------------------
# 3. Callers outside db/ do not reach into db.models mid-block
# ---------------------------------------------------------------------------


def test_no_db_models_calls_inside_a_raw_connection_block():
    """`core/`, `api/` and `sessions/` open raw connections in a handful of
    places. Calling a `db.models` accessor from inside one of those blocks is
    the realistic way this invariant gets broken."""
    opening = _connecting_functions(ast.parse((REPO_ROOT / "db" / "models.py").read_text()))
    # Module aliases that resolve to db.models across the tree.
    aliases = {"db", "models", "db_models"}
    offenders = []
    for rel, tree in _iter_sources():
        if rel.startswith("db/"):
            continue
        for connector, block in _connection_blocks(tree):
            for child in block.body:
                for node in ast.walk(child):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                        continue
                    owner = node.func.value
                    if isinstance(owner, ast.Name) and owner.id in aliases and node.func.attr in opening:
                        offenders.append(
                            f"{_location(rel, node)}: {owner.id}.{node.func.attr}() inside `with {connector}()`"
                        )
    assert not offenders, (
        "db.models accessor called inside a raw connection block:\n  "
        + "\n  ".join(offenders)
        + "\nIt reuses the same cached connection and commits on exit. Do the "
        "work in SQL on the open `conn`, or move the call outside the block."
    )


# ---------------------------------------------------------------------------
# 4. The one-active-goal predicate agrees between accessor and index
# ---------------------------------------------------------------------------


def test_active_goal_statuses_match_the_unique_index():
    """`create_goal`'s check and the v26 partial unique index must cover the
    same statuses. If they drift, either the index rejects goals the accessor
    considers creatable, or the accessor's check is narrower than the index
    and callers get IntegrityError-shaped `None`s they cannot explain."""
    import re

    models_src = (REPO_ROOT / "db" / "models.py").read_text()
    db_src = (REPO_ROOT / "db" / "database.py").read_text()

    index_match = re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS idx_session_goals_one_active.*?WHERE status IN \(([^)]*)\)",
        db_src,
        re.DOTALL,
    )
    assert index_match, "v26 one-active-goal unique index is missing from db/database.py"
    index_statuses = set(re.findall(r"'([^']+)'", index_match.group(1)))

    create_goal_src = models_src[models_src.index("def create_goal(") : models_src.index("def get_active_goal(")]
    accessor_match = re.search(r"status IN \(([^)]*)\)", create_goal_src)
    assert accessor_match, "create_goal no longer filters on goal status"
    accessor_statuses = set(re.findall(r"'([^']+)'", accessor_match.group(1)))

    assert accessor_statuses == index_statuses, (
        f"create_goal checks {sorted(accessor_statuses)} but the v26 unique index covers "
        f"{sorted(index_statuses)} — they must be identical."
    )
