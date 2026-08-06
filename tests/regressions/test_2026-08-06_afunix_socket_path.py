"""Regression: RLM child sockets failed to bind under deep run directories.

Shipped defect (plan §10.11, fixed 2026-08-06): ChildREPL placed exec.sock/
llm.sock inside the run dir, and AF_UNIX sun_path caps at ~104 bytes on
macOS/BSD — so any deep run dir (pytest tmpdirs, user-chosen workspace
roots) made bind() fail with "AF_UNIX path too long", taking the entire RLM
engine (and its test suite) down on such machines. Related: macOS rejects
RLIMIT_AS in forked children (EINVAL), which the socket failure had masked.

Fix: resolve_socket_dir() falls back to a short private mkdtemp under /tmp
(never TMPDIR — on macOS TMPDIR itself is the deep path), removed by
cleanup(); the engine and broker both take the child's resolved path; the
address-space cap became best-effort (defense-in-depth per I7).
"""

from pathlib import Path

from core.extensions.rlm.child_env import _SUN_PATH_MAX, resolve_socket_dir


def test_short_run_dir_keeps_sockets_in_place(tmp_path):
    short = Path("/tmp/pnx-short")
    sock_dir, is_temp = resolve_socket_dir(short)
    # resolve() follows /tmp -> /private/tmp on macOS; compare resolved.
    assert sock_dir == short.resolve()
    assert not is_temp


def test_deep_run_dir_falls_back_to_short_tmp(tmp_path):
    deep = tmp_path / ("x" * 120)
    sock_dir, is_temp = resolve_socket_dir(deep)
    assert is_temp
    assert sock_dir != deep
    assert len(str(sock_dir / "exec.sock").encode()) <= _SUN_PATH_MAX
    assert str(sock_dir).startswith("/tmp/")
    sock_dir.rmdir()  # created by mkdtemp; this test owns its cleanup


def test_child_repl_uses_resolved_dir(tmp_path):
    from core.extensions.rlm.child_env import ChildREPL

    deep = tmp_path / ("y" * 120)
    c = ChildREPL(deep)  # never started; construction resolves paths
    assert len(str(c.exec_sock_path).encode()) <= _SUN_PATH_MAX
    assert c.llm_sock_path.parent == c.exec_sock_path.parent
    assert c.run_dir == deep.resolve()  # run dir itself is unchanged
    c.cleanup()  # removes the private socket dir
    assert not c.exec_sock_path.parent.exists()
