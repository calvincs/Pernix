"""A bare filename in a space session could be captured by /tmp.

With a space workspace-home active, `_resolve_within` preferred whichever
root already CONTAINED the name — and it scanned every root, including
/tmp for writes and skills/.tool_output/kernels for reads. bash leaves
scratch files in /tmp, so `file_write("notes.md")` silently overwrote
/tmp/notes.md and `file_read("notes.md")` read it back, instead of using
the session's own workspace. Contradicts allowed_read_roots' own contract.

Relative names now resolve only against the space home and the global
workspace; every other root stays reachable by absolute path.
"""

import pytest

from core.tools import paths


@pytest.fixture
def space_home(tmp_path, monkeypatch):
    home = tmp_path / "workspace" / "spaces" / "alpha"
    ws = tmp_path / "workspace"
    home.mkdir(parents=True)
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))
    token = paths.WORKSPACE_HOME.set(str(home))
    yield home, ws
    paths.WORKSPACE_HOME.reset(token)


def test_a_bare_write_is_not_captured_by_an_unrelated_tmp_file(space_home, tmp_path):
    home, _ws = space_home
    decoy = paths.Path("/tmp/pernix-regression-notes.md")
    decoy.write_text("someone else's scratch file")
    try:
        resolved = paths.safe_write_path("pernix-regression-notes.md")
        assert resolved == home / "pernix-regression-notes.md"
        assert decoy.read_text() == "someone else's scratch file", "the /tmp file must be untouched"
    finally:
        decoy.unlink(missing_ok=True)


def test_a_bare_name_still_prefers_an_existing_global_workspace_file(space_home):
    home, ws = space_home
    (ws / "SYSTEM-MAP.md").write_text("the shared map")
    assert paths.safe_read_path("SYSTEM-MAP.md") == ws / "SYSTEM-MAP.md"


def test_a_new_bare_name_defaults_into_the_space_home(space_home):
    home, _ws = space_home
    assert paths.safe_write_path("fresh-note.md") == home / "fresh-note.md"


def test_an_absolute_tmp_path_still_works(space_home):
    target = paths.Path("/tmp/pernix-regression-abs.md")
    try:
        assert paths.safe_write_path(str(target)) == target
    finally:
        target.unlink(missing_ok=True)
