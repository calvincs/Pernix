"""Agent Mesh build, 2026-09-08: a NEW file written from a space session
always fell to the space home, even when the directory it named already
existed at the workspace root.

`file_write("spaces/agent-mesh/impl/Makefile")` from a session whose home is
<workspace>/spaces/agent-mesh produced
<workspace>/spaces/agent-mesh/spaces/agent-mesh/impl/Makefile — a phantom tree
that reads correctly in every listing until you notice the doubling. Two
orphan files were left behind, and the agent then burned rounds trying to
delete them.

Two rules fix it: a new file prefers the root whose containing directory
already exists, and a relative path that repeats the home's own prefix
resolves against the workspace root rather than nesting the space in itself.
"""

import pytest

from core.tools import paths


@pytest.fixture
def space_home(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    home = ws / "spaces" / "agent-mesh"
    home.mkdir(parents=True)
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))
    token = paths.WORKSPACE_HOME.set(str(home))
    yield home, ws
    paths.WORKSPACE_HOME.reset(token)


# ── the field case ───────────────────────────────────────────────────────────


def test_a_doubled_prefix_lands_in_the_real_directory(space_home):
    """Verbatim from session b72959f8407d."""
    home, ws = space_home
    (ws / "spaces" / "agent-mesh" / "impl").mkdir(parents=True, exist_ok=True)
    resolved = paths.safe_write_path("spaces/agent-mesh/impl/Makefile")
    assert resolved == ws / "spaces" / "agent-mesh" / "impl" / "Makefile"
    assert "agent-mesh/spaces/agent-mesh" not in str(resolved)


def test_a_doubled_prefix_is_redirected_even_with_no_existing_directory(space_home):
    """Nobody means to nest a space inside itself, so the guard applies even
    when neither candidate's parent exists yet."""
    _home, ws = space_home
    resolved = paths.safe_write_path("spaces/agent-mesh/brand-new/x.md")
    assert resolved == ws / "spaces" / "agent-mesh" / "brand-new" / "x.md"


def test_the_guard_beats_an_orphan_tree_that_already_exists(space_home):
    """Caught live on the box: once the mistake has been made, the nested tree
    is real, so "this path exists" and "its parent exists" both point at the
    orphan. The prefix has to win over both or every later write compounds it.
    """
    home, ws = space_home
    orphan = home / "spaces" / "agent-mesh" / "impl"
    orphan.mkdir(parents=True)
    (orphan / "Makefile").write_text("written by the bug")
    (ws / "spaces" / "agent-mesh" / "impl").mkdir(parents=True, exist_ok=True)

    resolved = paths.safe_write_path("spaces/agent-mesh/impl/Makefile")
    assert resolved == ws / "spaces" / "agent-mesh" / "impl" / "Makefile"
    assert (orphan / "Makefile").read_text() == "written by the bug", "the orphan must be left untouched"


def test_reads_of_a_doubled_path_are_redirected_too(space_home):
    """Otherwise a write lands in the right place and the read that verifies it
    finds the stale orphan instead."""
    home, ws = space_home
    orphan = home / "spaces" / "agent-mesh" / "impl"
    orphan.mkdir(parents=True)
    (orphan / "notes.md").write_text("stale")
    real = ws / "spaces" / "agent-mesh" / "impl"
    real.mkdir(parents=True, exist_ok=True)
    (real / "notes.md").write_text("current")

    assert paths.safe_read_path("spaces/agent-mesh/impl/notes.md") == real / "notes.md"


# ── the general rule: a new file lands next to its siblings ──────────────────


def test_a_new_file_prefers_the_root_whose_directory_exists(space_home):
    _home, ws = space_home
    (ws / "shared-pkg").mkdir()
    assert paths.safe_write_path("shared-pkg/new.py") == ws / "shared-pkg" / "new.py"


def test_the_space_home_still_wins_when_it_holds_the_directory(space_home):
    home, ws = space_home
    (home / "impl").mkdir()
    (ws / "impl").mkdir()
    # Home is scanned first, so a directory present in both stays local.
    assert paths.safe_write_path("impl/local.py") == home / "impl" / "local.py"


# ── nothing the earlier space-path fix established was broken ───────────────


def test_a_new_bare_name_still_defaults_into_the_space_home(space_home):
    home, _ws = space_home
    assert paths.safe_write_path("fresh-note.md") == home / "fresh-note.md"


def test_an_existing_workspace_file_is_still_preferred(space_home):
    _home, ws = space_home
    (ws / "SYSTEM-MAP.md").write_text("the shared map")
    assert paths.safe_read_path("SYSTEM-MAP.md") == ws / "SYSTEM-MAP.md"


def test_an_absolute_path_is_untouched(space_home):
    _home, ws = space_home
    target = ws / "absolute.md"
    assert paths.safe_write_path(str(target)) == target


def test_a_bare_write_is_still_not_captured_by_tmp(space_home):
    """The 2026-09-01 fix must survive: /tmp never competes for a bare name."""
    home, _ws = space_home
    decoy = paths.Path("/tmp/pernix-regression-doubling.md")
    decoy.write_text("someone else's scratch file")
    try:
        assert paths.safe_write_path("pernix-regression-doubling.md") == home / "pernix-regression-doubling.md"
        assert decoy.read_text() == "someone else's scratch file"
    finally:
        decoy.unlink(missing_ok=True)
