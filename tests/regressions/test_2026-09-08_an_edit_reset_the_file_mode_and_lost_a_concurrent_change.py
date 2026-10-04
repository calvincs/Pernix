"""Harness review 3.2.1 (H13), 2026-09-08: every write through file_edit,
multiedit and file_write handed the file back at mode 0600, and two edits to
the same file could both report success while one of them was erased.

Both paths built the replacement with `tempfile.mkstemp`, which creates its
file at 0600 regardless of umask, and then `os.replace`d it over the target
without ever looking at the mode the target had. Measured, not inferred:
0755 -> 0600 (the script stops being executable), 0640 -> 0600 (the group
loses its read), 0644 -> 0600 (every other uid loses its read), 02755 -> 0600
(setgid gone). A `chmod +x` after every edit is not a workflow anyone asked
for.

The `fcntl.flock` in both writers was taken on the freshly-mkstemp'd temp fd —
a file no other process has ever been able to name — so it could not block
anything. 60/60 unsynchronized trials of two threads editing two distinct
lines of one file lost an edit, and both calls returned a success diff.

The fix stats the target and fchmods the temp file to its mode before the
replace, and holds a lock on the canonical target across the whole
read-transform-replace interval. The lock is process-local: a shell `sed -i`
or a second Pernix process is still not coordinated, which is why the edit
also re-reads the file immediately before replacing it and refuses when the
bytes moved under it.
"""

import hashlib
import os
import stat
import subprocess
import threading

import pytest

from core.tools.builtin import file_edit as file_edit_mod
from core.tools.builtin.core_tools import file_write
from core.tools.builtin.file_edit import file_edit, multiedit


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("config.settings.workspace_dir", str(root))
    return root


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def seed(ws, name: str, mode: int, body: str = "#!/bin/sh\necho hello\n"):
    p = ws / name
    p.write_text(body)
    os.chmod(p, mode)
    if mode_of(p) != mode:  # pragma: no cover - filesystem/policy dependent
        pytest.skip(f"filesystem would not hold mode {mode:o}")
    return p


# ── the field case: an edited file keeps the mode it had ────────────────────


@pytest.mark.parametrize("mode", [0o755, 0o644, 0o640, 0o600, 0o444])
def test_file_edit_keeps_the_mode_the_file_had(ws, mode):
    p = seed(ws, "thing.sh", mode)
    result = file_edit("thing.sh", "echo hello", "echo goodbye")
    assert result.startswith("Edited"), result
    assert "echo goodbye" in p.read_text()
    assert mode_of(p) == mode, f"{mode_of(p):04o} != {mode:04o}"


@pytest.mark.parametrize("mode", [0o755, 0o644, 0o640])
def test_file_write_keeps_the_mode_the_file_had(ws, mode):
    p = seed(ws, "thing.sh", mode)
    result = file_write("thing.sh", "#!/bin/sh\necho other\n")
    assert result.startswith("Written"), result
    assert mode_of(p) == mode


@pytest.mark.parametrize("mode", [0o755, 0o640])
def test_multiedit_keeps_the_mode_the_file_had(ws, mode):
    p = seed(ws, "thing.sh", mode)
    result = multiedit("thing.sh", [{"old_string": "echo hello", "new_string": "echo zzz"}])
    assert result.startswith("Applied"), result
    assert mode_of(p) == mode


def test_an_edited_script_can_still_be_executed(ws):
    """Ported from the H13 reproduction: the 0600 result was not merely a
    cosmetic mode change — direct exec raised EACCES."""
    p = seed(ws, "run.sh", 0o755)
    before = subprocess.run([str(p)], capture_output=True, text=True)
    if before.returncode != 0:  # pragma: no cover - noexec mount
        pytest.skip("this filesystem cannot execute the script even before the edit")
    assert before.stdout.strip() == "hello"

    file_edit("run.sh", "echo hello", "echo goodbye")

    assert os.access(p, os.X_OK), "the edit dropped the executable bit"
    after = subprocess.run([str(p)], capture_output=True, text=True)
    assert after.returncode == 0
    assert after.stdout.strip() == "goodbye"


# ── privilege bits: setgid is carried, setuid is not ────────────────────────


def test_setgid_survives_an_edit(ws):
    p = seed(ws, "shared.sh", 0o2755)
    file_edit("shared.sh", "echo hello", "echo q")
    assert mode_of(p) == 0o2755, f"{mode_of(p):04o}"


def test_setuid_is_dropped_and_said_out_loud(ws):
    """Deliberate asymmetry. setgid's blast radius is a group the file already
    belonged to and shared build trees rely on it; setuid hands an arbitrary
    uid — usually root — to bytes the agent just authored."""
    p = seed(ws, "priv.sh", 0o4755)
    result = file_edit("priv.sh", "echo hello", "echo q")
    assert mode_of(p) == 0o0755, f"{mode_of(p):04o}"
    assert "setuid" in result


# ── new files get a deliberate default, not whatever mkstemp left ───────────


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda: file_edit("fresh.txt", "", "body\n"), id="file_edit"),
        pytest.param(lambda: file_write("fresh.txt", "body\n"), id="file_write"),
    ],
)
def test_a_new_file_gets_the_declared_default_mode(ws, write):
    from core.tools.atomic import NEW_FILE_MODE

    result = write()
    assert not result.startswith("Error"), result
    assert mode_of(ws / "fresh.txt") == NEW_FILE_MODE
    assert NEW_FILE_MODE == 0o600, "the default is deliberate; changing it is a decision, not a drift"


# ── a failed write leaves nothing behind ────────────────────────────────────


def test_a_failed_replace_leaves_no_temp_file_and_no_damage(ws, monkeypatch):
    p = seed(ws, "keep.txt", 0o644, "original\n")

    def boom(*_a, **_kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", boom)
    result = file_edit("keep.txt", "original", "replacement")
    monkeypatch.undo()

    assert result.startswith("Error"), result
    assert p.read_text() == "original\n"
    assert mode_of(p) == 0o644
    assert list(ws.glob(".keep.txt.*")) == [], "temp file left behind"


# ── two cooperating editors serialize instead of both winning ───────────────


def test_two_barrier_controlled_edits_both_survive(ws, monkeypatch):
    """The exact barrier: both threads rendezvous *inside _apply_edit*, which
    is the point at which each one has finished its read and has not yet
    written. Before the fix both threads met there, both wrote, and the
    second replace erased the first edit.

    After the fix the second thread cannot reach _apply_edit at all until the
    first has replaced the file, so the rendezvous times out and the barrier
    breaks — that break is the observable proof that the two edits serialized
    rather than overlapped.
    """
    target = ws / "module.py"
    lines = [f"# filler {i}" for i in range(40)]
    lines[5] = "ALPHA_MARKER = 1"
    lines[35] = "BETA_MARKER = 2"
    target.write_text("\n".join(lines) + "\n")

    barrier = threading.Barrier(2)
    real_apply = file_edit_mod._apply_edit

    def rendezvous(*args, **kwargs):
        try:
            barrier.wait(timeout=0.75)
        except threading.BrokenBarrierError:
            pass
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(file_edit_mod, "_apply_edit", rendezvous)

    said: dict[str, str] = {}

    def run(name, old, new):
        said[name] = file_edit("module.py", old, new)

    threads = [
        threading.Thread(target=run, args=("alpha", "ALPHA_MARKER = 1", "ALPHA_DONE = 1")),
        threading.Thread(target=run, args=("beta", "BETA_MARKER = 2", "BETA_DONE = 2")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "an edit deadlocked on the target lock"

    body = target.read_text()
    assert "ALPHA_DONE" in body, f"alpha's edit was lost; alpha said {said.get('alpha', '')[:120]!r}"
    assert "BETA_DONE" in body, f"beta's edit was lost; beta said {said.get('beta', '')[:120]!r}"
    assert barrier.broken, "both edits reached the transform together — they did not serialize"


# ── an external change present at validation is rejected ────────────────────


def test_an_external_change_during_the_edit_is_refused(ws, monkeypatch):
    """The residual race the lock cannot close: another process. Nothing
    coordinates a shell `sed -i`, so the edit re-reads the file immediately
    before replacing it and refuses when the bytes moved. A write landing
    between that check and the replace is still lost — disclosed, not fixed.
    """
    target = ws / "conf.py"
    target.write_text("VALUE = 1\nOTHER = 2\n")
    real_apply = file_edit_mod._apply_edit

    def meddle(*args, **kwargs):
        target.write_text("VALUE = 1\nOTHER = 99\n")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(file_edit_mod, "_apply_edit", meddle)

    result = file_edit("conf.py", "VALUE = 1", "VALUE = 42")
    assert result.startswith("Error"), result
    assert "changed on disk" in result
    assert target.read_text() == "VALUE = 1\nOTHER = 99\n", "the outsider's write was clobbered"


def test_multiedit_also_refuses_an_external_change(ws, monkeypatch):
    target = ws / "conf.py"
    target.write_text("VALUE = 1\nOTHER = 2\n")
    real_apply = file_edit_mod._apply_edit

    def meddle(*args, **kwargs):
        target.write_text("VALUE = 1\nOTHER = 99\n")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(file_edit_mod, "_apply_edit", meddle)

    result = multiedit("conf.py", [{"old_string": "VALUE = 1", "new_string": "VALUE = 42"}])
    assert result.startswith("Error"), result
    assert "changed on disk" in result
    assert target.read_text() == "VALUE = 1\nOTHER = 99\n"


# ── file_write's revision precondition for read-modify-write ────────────────


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_file_write_refuses_a_stale_expected_revision(ws):
    p = ws / "notes.md"
    p.write_text("v1\n")
    result = file_write("notes.md", "v2\n", expected_sha256=sha("v0\n"))
    assert result.startswith("Error"), result
    assert p.read_text() == "v1\n"
    assert sha("v1\n") in result, "the rejection must name the revision that is actually there"


def test_file_write_accepts_the_current_revision(ws):
    p = ws / "notes.md"
    p.write_text("v1\n")
    result = file_write("notes.md", "v2\n", expected_sha256=sha("v1\n"))
    assert result.startswith("Written"), result
    assert p.read_text() == "v2\n"


def test_file_write_can_demand_the_file_be_absent(ws):
    result = file_write("brand-new.md", "first\n", expected_sha256="absent")
    assert result.startswith("Written"), result
    again = file_write("brand-new.md", "second\n", expected_sha256="absent")
    assert again.startswith("Error"), again
    assert (ws / "brand-new.md").read_text() == "first\n"


def test_file_write_without_a_precondition_is_unchanged(ws):
    p = ws / "notes.md"
    p.write_text("v1\n")
    result = file_write("notes.md", "v2\n")
    assert result.startswith("Written")
    assert p.read_text() == "v2\n"


# ── nothing the existing write contract promised was broken ─────────────────


def test_line_endings_are_still_preserved_through_the_shared_writer(ws):
    p = ws / "crlf.txt"
    p.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    os.chmod(p, 0o644)
    assert file_edit("crlf.txt", "line2", "LINE2").startswith("Edited")
    assert p.read_bytes() == b"line1\r\nLINE2\r\nline3\r\n"
    assert mode_of(p) == 0o644


def test_multiedit_is_still_all_or_nothing(ws):
    p = seed(ws, "batch.txt", 0o644, "aaa bbb ccc\n")
    result = multiedit(
        "batch.txt",
        [
            {"old_string": "aaa", "new_string": "111"},
            {"old_string": "zzz_no_match", "new_string": "222"},
        ],
    )
    assert "Edit 2 failed" in result
    assert p.read_text() == "aaa bbb ccc\n"
    assert mode_of(p) == 0o644
