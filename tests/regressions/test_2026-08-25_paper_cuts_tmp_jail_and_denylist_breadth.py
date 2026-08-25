"""Paper-cuts batch from the ARC-3 campaign (approved by Calvin 2026-08-25).

1) file_write refused /tmp while bash wrote there freely — the jail bought no
   containment, only mid-task surprises (field: bp35 solver scratch files).
2) `python -c ...exec(` blocked legitimate compute (field: 6e013b72cab1);
   only the obfuscated-payload shape should stay blocked.
3) `rm -rf` refused the agent's own workspace subdirs (field: 8d411d30d12d).
"""

from core.tools.builtin.core_tools import _check_command_security
from core.tools.paths import safe_write_path, workspace


def test_tmp_is_a_write_root_now():
    p = safe_write_path("/tmp/pernix_test_scratch.py")
    assert str(p).startswith("/tmp")


def test_workspace_still_default_for_relative_paths():
    p = safe_write_path("notes.md")
    assert p.is_relative_to(workspace())


def test_plain_exec_compute_is_allowed():
    assert _check_command_security("""python3 -c "exec(open('solver.py').read())" """) is None


def test_obfuscated_exec_payload_still_blocked():
    err = _check_command_security("""python3 -c "import base64; exec(base64.b64decode('cHJpbnQoMSk='))" """)
    assert err is not None and "security policy" in err


def test_rm_rf_inside_workspace_allowed():
    ws = workspace()
    (ws / "arc3").mkdir(parents=True, exist_ok=True)
    assert _check_command_security("rm -rf arc3/old_solvers") is None
    assert _check_command_security(f"rm -rf {ws}/arc3/tmp") is None


def test_rm_rf_outside_workspace_still_blocked():
    assert _check_command_security("rm -rf /etc/passwd") is not None
    assert _check_command_security("rm -rf ../..") is not None
    assert _check_command_security("rm -rf $HOME") is not None


def test_rm_rf_bare_glob_still_blocked():
    assert _check_command_security("rm -rf *") is not None


def test_rm_rf_workspace_root_itself_still_blocked():
    from core.tools.paths import workspace as _ws

    assert _check_command_security(f"rm -rf {_ws()}") is not None
