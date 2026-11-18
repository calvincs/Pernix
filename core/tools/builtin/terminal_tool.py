"""Pernix — Interactive terminal tool: one `terminal` tool, several actions.

`bash` runs one command and returns. `job_start` keeps a command running but
can only be read, never typed into. Neither can hold a program that wants a
conversation: an ssh login, a REPL, an installer that asks questions, a
full-screen text UI. Agents that met one built the missing layer by hand — a
daemon to hold the connection, a socket to send it keys, a parser to turn
terminal escape codes back into a screen — and spent most of their rounds on
that plumbing instead of the task.

This tool is that layer, owned by the harness:

- `open` starts a command on a real pseudo-terminal, so the program believes
  it has a terminal (colors, cursor movement, password prompts, job control).
- `send` types text and named keys, then returns the screen once output
  settles (or once a `wait_for` pattern shows up).
- `screen` reads the current screen as plain text, the way a person would see
  it, plus optional scrollback.
- `resize`, `list`, `close` do what they say.

The screen is kept by a terminal emulator (pyte), fed by a reader thread, so a
read is always the latest full frame, not a byte log to re-parse.

Admission is the same as bash and job_start (`check_shell_command`), with the
same environment and rlimits: a third launcher must not be the way around the
policy of the first two.

Lifetime: a terminal survives the end of the turn and a cancelled turn, like a
job. It ends when the program exits, on `close`, after `terminal_idle_timeout_s`
without a tool call, after `terminal_max_lifetime_s`, when its session is
deleted, or when the server stops. Terminals live in server memory, so a
server restart ends them; the raw output log on disk stays.
"""

from __future__ import annotations

import atexit
import fcntl
import logging
import os
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid

from config import settings
from core.tools.interactive import KEY_HELP, parse_input, same_family, terminal_bytes

logger = logging.getLogger("pernix.tools.terminal")

_MIN_COLS, _MAX_COLS = 20, 300
_MIN_ROWS, _MAX_ROWS = 5, 120
_HISTORY_LINES = 5000
_SETTLE_S = 0.35  # quiet time that counts as "the screen has stopped changing"
_DEFAULT_WAIT_S = 5.0
_MAX_WAIT_S = 60.0
_LOG_CAP_BYTES = 50 * 1024 * 1024
_DONE_KEEP_S = 1800  # how long an exited terminal's last screen stays readable
_DECCKM = 1 << 5  # pyte stores private mode ?1 (application cursor keys) shifted by 5

_terminals: dict[str, "_Terminal"] = {}
_registry_lock = threading.Lock()


def _session_members(sid: int) -> list[int]:
    """Live pids whose kernel session id is `sid` (the terminal's containment unit)."""
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = open(f"/proc/{entry}/stat").read()
            rest = stat[stat.rindex(")") + 2 :].split()
            if rest[0] != "Z" and int(rest[3]) == sid:
                out.append(int(entry))
        except (OSError, ValueError, IndexError):
            continue
    return out


def _exit_text(code: int | None) -> str:
    if code is None:
        return ""
    if code < 0:
        return f" (killed by signal {-code})"
    return f" code {code}"


class _Terminal:
    """One program on one pseudo-terminal, with an emulated screen."""

    def __init__(self, term_id, owner, name, command, cwd, cols, rows, proc, master_fd, log_path):
        import pyte

        self.id = term_id
        self.owner = owner
        self.name = name
        self.command = command
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.proc = proc
        self.master_fd = master_fd
        self.log_path = log_path
        self.cond = threading.Condition()
        term = self

        class _Screen(pyte.HistoryScreen):
            # Programs ask the terminal questions (cursor position, device
            # attributes) and block until it answers. The emulator's answer
            # goes back down the pty like a real terminal's would.
            def write_process_input(self, data):
                term._write(data.encode("utf-8", "replace"))

        self.screen = _Screen(cols, rows, history=_HISTORY_LINES, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)
        now = time.monotonic()
        self.opened_at = now
        self.last_used = now
        self.last_output = now
        self.bytes_total = 0
        self.bytes_reported = 0
        self.exit_code: int | None = None
        self.done = False
        self.end_reason = ""
        self.done_at = 0.0
        self.feed_errors = 0
        self._log_fh = None
        try:
            self._log_fh = open(log_path, "ab")
        except OSError:
            pass
        self._thread = threading.Thread(target=self._read_loop, name=f"terminal-{term_id}", daemon=True)
        self._thread.start()

    # -- I/O -----------------------------------------------------------------

    def _write(self, data: bytes) -> None:
        if self.done or not data:
            return
        view = memoryview(data)
        deadline = time.monotonic() + 5.0
        while view and time.monotonic() < deadline:
            try:
                n = os.write(self.master_fd, view)
                view = view[n:]
            except BlockingIOError:
                time.sleep(0.01)
            except OSError:
                return

    def _read_loop(self) -> None:
        fd = self.master_fd
        idle_cap = float(settings.terminal_idle_timeout_s or 0)
        life_cap = float(settings.terminal_max_lifetime_s or 0)
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 1.0)
            except (OSError, ValueError):
                break
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""  # EIO: every holder of the slave side is gone
                if not data:
                    break
                with self.cond:
                    try:
                        self.stream.feed(data)
                    except Exception as e:  # an escape sequence the emulator rejects
                        self.feed_errors += 1
                        if self.feed_errors <= 3:
                            logger.warning("terminal %s: emulator skipped a chunk: %s", self.id, e)
                    self.bytes_total += len(data)
                    self.last_output = time.monotonic()
                    self.cond.notify_all()
                if self._log_fh is not None:
                    try:
                        if self.bytes_total <= _LOG_CAP_BYTES:
                            self._log_fh.write(data)
                            self._log_fh.flush()
                    except (OSError, ValueError):
                        pass
                continue
            # Quiet second: bookkeeping.
            if self.proc.poll() is not None:
                break  # leader gone and nothing left to read
            now = time.monotonic()
            if idle_cap > 0 and now - self.last_used > idle_cap:
                self.end_reason = f"closed after {int(idle_cap)}s without a tool call"
                break
            if life_cap > 0 and now - self.opened_at > life_cap:
                self.end_reason = f"closed at the {int(life_cap)}s lifetime cap"
                break
        self._finish()

    def _finish(self) -> None:
        self._terminate()
        try:
            code = self.proc.wait(timeout=3)
        except Exception:
            code = None
        with self.cond:
            self.exit_code = code
            self.done = True
            self.done_at = time.monotonic()
            self.cond.notify_all()
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
        logger.info("terminal %s ended (exit %s%s)", self.id, code, f", {self.end_reason}" if self.end_reason else "")

    def _terminate(self) -> list[int]:
        """Stop everything in the terminal's session. Returns pids that survived."""
        sid = self.proc.pid  # session leader: start_new_session made pid == sid
        for sig, grace in ((signal.SIGHUP, 0.5), (signal.SIGTERM, 1.0), (signal.SIGKILL, 1.0)):
            members = _session_members(sid)
            if not members:
                return []
            for pid in members:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                self.proc.poll()  # reap the leader so it stops counting
                if not _session_members(sid):
                    return []
                time.sleep(0.05)
        return _session_members(sid)

    def close(self, reason: str) -> None:
        if not self.done and not self.end_reason:
            self.end_reason = reason
        leftovers = self._terminate()
        if leftovers:
            self.end_reason += f" (could not stop pids {leftovers})"
        self._thread.join(timeout=5)

    # -- input ---------------------------------------------------------------

    def send(self, steps: list[tuple[str, object]]) -> None:
        for kind, value in steps:
            if kind == "wait":
                time.sleep(float(value))
            elif kind == "text":
                self._write(str(value).encode("utf-8", "replace"))
            else:
                with self.cond:
                    app_cursor = _DECCKM in self.screen.mode
                self._write(terminal_bytes(str(value), app_cursor))

    def resize(self, cols: int, rows: int) -> None:
        with self.cond:
            try:
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass
            self.screen.resize(lines=rows, columns=cols)
            self.cols, self.rows = cols, rows

    # -- output --------------------------------------------------------------

    def _screen_lines(self) -> list[str]:
        raw_lines = list(self.screen.display)
        lines = [ln.rstrip() for ln in raw_lines]
        cur = self.screen.cursor
        # Keep the blanks between the text and the cursor on the cursor's own
        # row: a prompt such as ">>> " ends in a space, and a wait_for written
        # the natural way ('>>> $') has to be able to see it.
        if 0 <= cur.y < len(lines) and cur.x > len(lines[cur.y]):
            lines[cur.y] = raw_lines[cur.y][: cur.x]
        keep = max(cur.y + 1, 1)
        while len(lines) > keep and not lines[-1]:
            lines.pop()
        return lines

    def _history_lines(self, n: int) -> tuple[list[str], int]:
        top = list(self.screen.history.top)
        total = len(top)
        picked = top[-n:] if n > 0 else []
        out = []
        for row in picked:
            out.append("".join(row[x].data for x in range(self.cols)).rstrip())
        return out, total

    def wait(self, pattern: re.Pattern | None, timeout: float, cancel_event=None, after_input: bool = False) -> str:
        """Block until the screen settles or `pattern` shows up. Returns a note.

        after_input: the caller just typed something. The screen still shows
        what it showed before the program reacted, so a pattern that already
        matched (the old prompt) is not an answer yet: wait for new output, or
        for a short grace in case the program has nothing more to say.
        """
        start = time.monotonic()
        deadline = start + timeout
        with self.cond:
            start_bytes = self.bytes_total
            while True:
                now = time.monotonic()
                if pattern is not None:
                    fresh = not after_input or self.bytes_total > start_bytes or now - start >= 0.5
                    if fresh and pattern.search("\n".join(self._screen_lines())):
                        return "wait_for matched"
                    if self.done:
                        return "program exited before the pattern appeared"
                    if now >= deadline:
                        return f"pattern not seen after {timeout:g}s"
                else:
                    if now - max(self.last_output, start) >= _SETTLE_S:
                        return ""
                    if self.done:
                        return ""
                    if now >= deadline:
                        return f"output still changing after {timeout:g}s"
                if cancel_event is not None and cancel_event.is_set():
                    return "wait cut short: the call was cancelled"
                self.cond.wait(min(0.1, max(0.01, deadline - now)))

    def render(self, scrollback: int = 0, note: str = "") -> str:
        with self.cond:
            lines = self._screen_lines()
            hist, hist_total = self._history_lines(max(0, scrollback))
            cur = self.screen.cursor
            new_bytes = self.bytes_total - self.bytes_reported
            self.bytes_reported = self.bytes_total
            if self.done:
                state = "exited" + _exit_text(self.exit_code)
                if self.end_reason:
                    state = self.end_reason
            else:
                state = f"running pid {self.proc.pid}"
            head = [f"terminal {self.id}"]
            if self.name:
                head.append(f'"{self.name}"')
            head += [
                state,
                f"{self.cols}x{self.rows}",
                f"cursor row {cur.y + 1} col {cur.x + 1}",
                f"{new_bytes:,} new bytes" if new_bytes else "no new output",
            ]
        parts = ["[" + " | ".join(head) + "]"]
        if note:
            parts.append(f"[{note}]")
        if hist:
            parts.append(f"--- scrollback (last {len(hist)} of {hist_total} lines above the screen) ---")
            parts.extend(hist)
            parts.append("--- screen ---")
        parts.extend(lines if any(lines) else ["(screen is empty)"])
        tail = []
        if not hist and hist_total:
            tail.append(f"{hist_total} line(s) scrolled off the top: pass scrollback=N to read them")
        if self.feed_errors:
            tail.append(
                f"{self.feed_errors} output chunk(s) used escape codes the emulator skipped; raw log: {self.log_path}"
            )
        if tail:
            parts.append("[" + "; ".join(tail) + "]")
        return "\n".join(parts)


# -- registry ----------------------------------------------------------------


def _sweep() -> None:
    """Forget exited terminals whose last screen has been readable long enough."""
    now = time.monotonic()
    with _registry_lock:
        for tid in [t for t, term in _terminals.items() if term.done and now - term.done_at > _DONE_KEEP_S]:
            _terminals.pop(tid, None)


def _visible(session_id: str) -> list[_Terminal]:
    with _registry_lock:
        terms = list(_terminals.values())
    return [t for t in terms if same_family(t.owner, session_id)]


def _resolve(term_id: str, session_id: str) -> tuple[_Terminal | None, str]:
    """Find the terminal a call means. An empty id works when there is exactly one."""
    term_id = (term_id or "").strip()
    visible = _visible(session_id)
    if term_id:
        for t in visible:
            if t.id == term_id or t.name == term_id:
                return t, ""
        with _registry_lock:
            exists = term_id in _terminals
        if exists:
            return None, f"Error: terminal {term_id} belongs to another session."
        listing = _format_list(visible)
        return None, f"Error: no terminal with id '{term_id}'. {listing}"
    live = [t for t in visible if not t.done]
    if len(live) == 1:
        return live[0], ""
    if not live and len(visible) == 1:
        return visible[0], ""
    if not visible:
        return None, "Error: no terminal is open. Start one with action='open' and a command."
    return None, "Error: several terminals are open — pass id. " + _format_list(visible)


def _format_list(terms: list[_Terminal]) -> str:
    if not terms:
        return "No terminals are open."
    rows = []
    for t in terms:
        state = (t.end_reason or "exited" + _exit_text(t.exit_code)) if t.done else f"running pid {t.proc.pid}"
        label = f' "{t.name}"' if t.name else ""
        rows.append(f"- {t.id}{label}: {state}, {t.cols}x{t.rows}, command: {t.command[:120]}")
    return "Terminals:\n" + "\n".join(rows)


def _clamp(value, lo, hi, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _wait_seconds(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(v, _MAX_WAIT_S))


def _compile(wait_for: str) -> tuple[re.Pattern | None, str]:
    if not wait_for:
        return None, ""
    try:
        return re.compile(wait_for, re.MULTILINE), ""
    except re.error as e:
        return (
            None,
            f"Error: wait_for is not a valid regular expression ({e}). Escape special characters or use plain words.",
        )


def _child_setup() -> None:
    """Runs in the child between fork and exec: bash's rlimits, then make the
    pty the controlling terminal (stdin is already the slave side)."""
    import resource

    from core.tools.builtin.jobs_tool import _rlimits

    as_limit, fsize = _rlimits()
    try:
        if as_limit > 0:
            resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
    except (ValueError, OSError):
        pass
    try:
        if fsize > 0:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    except (ValueError, OSError):
        pass
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


def _open(command, name, cols, rows, wait_for, wait_s, ctx) -> str:
    from core.tools.builtin.core_tools import check_shell_command
    from core.tools.paths import build_shell_env, roots_for_context

    command = (command or "").strip()
    if not command:
        return "Error: action='open' needs a command, e.g. command='bash' for a shell or 'ssh -p 22 host'."
    blocked = check_shell_command(command)
    if blocked:
        return blocked
    pattern, err = _compile(wait_for)
    if err:
        return err

    session_id = ctx.get("session_id", "") or "unknown"
    cap = int(settings.terminal_max_per_session)
    live = [t for t in _visible(session_id) if not t.done and t.owner == session_id]
    if len(live) >= cap:
        return f"Error: {cap} terminal(s) already open for this session — close one first.\n" + _format_list(live)

    ws_root, cwd = roots_for_context(ctx.get("workspace_override"), ctx.get("workspace_home"))
    cwd.mkdir(parents=True, exist_ok=True)
    env = build_shell_env(ws_root, cwd)
    env["TERM"] = "xterm-256color"
    env.pop("COLUMNS", None)
    env.pop("LINES", None)

    term_id = "t-" + uuid.uuid4().hex[:6]
    term_dir = ws_root / ".terminals" / term_id
    term_dir.mkdir(parents=True, exist_ok=True)
    log_path = term_dir / "output.log"

    master_fd, slave_fd = os.openpty()
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        proc = subprocess.Popen(
            ["/bin/bash", "-c", command],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
            preexec_fn=_child_setup,
            close_fds=True,
        )
    except OSError as e:
        os.close(master_fd)
        os.close(slave_fd)
        return f"Error: failed to start terminal: {e}"
    os.close(slave_fd)
    os.set_blocking(master_fd, False)

    term = _Terminal(
        term_id, session_id, name or "", command[:2000], str(cwd), cols, rows, proc, master_fd, str(log_path)
    )
    with _registry_lock:
        _terminals[term_id] = term
    logger.info("terminal %s opened (session %s, pid %d, cwd %s)", term_id, session_id[:12], proc.pid, cwd)
    note = term.wait(pattern, wait_s, ctx.get("_cancel_event"))
    opened = f"opened in {cwd}; use id='{term_id}' with action send / screen / close"
    return term.render(note="; ".join(n for n in (opened, note) if n))


def terminal(
    action: str = "",
    id: str = "",  # noqa: A002 - the name the model sees
    command: str = "",
    input: str = "",  # noqa: A002
    raw: bool = False,
    wait_for: str = "",
    wait_seconds: float | None = None,
    cols: int | None = None,
    rows: int | None = None,
    scrollback: int = 0,
    name: str = "",
    _context: dict | None = None,
) -> str:
    """Drive an interactive terminal program. See register() for the contract."""
    if not settings.terminal_enabled:
        return "Error: the terminal tool is disabled (settings.terminal_enabled)."
    ctx = dict(_context or {})
    session_id = ctx.get("session_id", "") or "unknown"
    action = (action or "").strip().lower()
    if not action:
        action = "open" if command else ("send" if input else "screen")
    _sweep()

    if action == "open":
        return _open(
            command,
            name,
            _clamp(cols, _MIN_COLS, _MAX_COLS, 120),
            _clamp(rows, _MIN_ROWS, _MAX_ROWS, 40),
            wait_for,
            _wait_seconds(wait_seconds, _DEFAULT_WAIT_S),
            ctx,
        )
    if action == "list":
        return _format_list(_visible(session_id))
    if action not in ("send", "screen", "resize", "close"):
        return f"Error: unknown action '{action}'. Use open, send, screen, resize, close or list."

    term, err = _resolve(id, session_id)
    if term is None:
        return err
    term.last_used = time.monotonic()
    pattern, err = _compile(wait_for)
    if err:
        return err
    back = _clamp(scrollback, 0, _HISTORY_LINES, 0)

    if action == "close":
        term.close("closed by the agent")
        text = term.render(scrollback=back, note="terminal closed; this is its last screen")
        with _registry_lock:
            _terminals.pop(term.id, None)
        return text

    if action == "resize":
        if term.done:
            return term.render(note="the program has exited; nothing to resize")
        term.resize(_clamp(cols, _MIN_COLS, _MAX_COLS, term.cols), _clamp(rows, _MIN_ROWS, _MAX_ROWS, term.rows))
        note = term.wait(None, _wait_seconds(wait_seconds, 2.0), ctx.get("_cancel_event"))
        return term.render(scrollback=back, note=note)

    if action == "send":
        if term.done:
            return term.render(scrollback=back, note="the program has exited; input was NOT sent. Open a new terminal.")
        steps = parse_input(input, raw=bool(raw))
        if not steps:
            return "Error: action='send' needs input, e.g. input='ls -la<enter>'. " + KEY_HELP
        term.send(steps)
        note = term.wait(
            pattern, _wait_seconds(wait_seconds, _DEFAULT_WAIT_S), ctx.get("_cancel_event"), after_input=True
        )
        return term.render(scrollback=back, note=note)

    # screen
    note = ""
    if pattern is not None or wait_seconds:
        note = term.wait(pattern, _wait_seconds(wait_seconds, _DEFAULT_WAIT_S), ctx.get("_cancel_event"))
    return term.render(scrollback=back, note=note)


def close_for_session(session_id: str) -> int:
    """End every terminal a session opened (session delete). Returns the count."""
    with _registry_lock:
        mine = [t for t in _terminals.values() if t.owner == session_id]
    for t in mine:
        try:
            t.close("its session was deleted")
        except Exception as e:
            logger.warning("terminal %s close failed: %s", t.id, e)
        with _registry_lock:
            _terminals.pop(t.id, None)
    return len(mine)


def close_all() -> None:
    """Server stop: no terminal may outlive the process that owns its screen."""
    with _registry_lock:
        terms = list(_terminals.values())
        _terminals.clear()
    for t in terms:
        try:
            t.end_reason = t.end_reason or "server stopped"
            t._terminate()
        except Exception:
            pass


atexit.register(close_all)


def register(reg) -> None:
    if not settings.terminal_enabled:
        return
    try:
        import pyte  # noqa: F401
    except ImportError:
        logger.info("pyte not installed — terminal tool not available")
        return
    reg.register(
        name="terminal",
        func=terminal,
        description=(
            "Drive an INTERACTIVE terminal program and read its screen as plain text: ssh "
            "sessions, REPLs, installers that ask questions, debuggers, full-screen text UIs "
            "(menus, editors, games), or any script of your own that talks on stdin/stdout. "
            "The program runs on a real pseudo-terminal and stays open between calls and "
            "across turns. Use bash for commands that just run and finish, job_start for long "
            "compute you only need to read; use this when the program waits for you. "
            "Do NOT build your own daemon, socket, FIFO or escape-code parser for this. "
            "Actions: open (command) -> returns an id and the first screen; send (input) -> "
            "types, then returns the screen once output settles; screen -> current screen; "
            "resize (cols, rows); list; close. " + KEY_HELP + " "
            "Every result is the full current screen, so one send per step is enough. "
            "Same command policy and environment as bash."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["open", "send", "screen", "resize", "list", "close"],
                    "description": "What to do.",
                },
                "id": {
                    "type": "string",
                    "description": "Terminal id from open (or its name). May be omitted when only one terminal is open.",
                },
                "command": {
                    "type": "string",
                    "description": "open: shell command to start, e.g. 'bash', 'python3', 'ssh -p 22 user@host'.",
                },
                "input": {
                    "type": "string",
                    "description": "send: text and keys to type, e.g. 'ls -la<enter>' or '<esc>:wq<enter>' or '<ctrl-c>'.",
                },
                "raw": {
                    "type": "boolean",
                    "description": "send: type input exactly as written, with no <key> parsing. Default false.",
                },
                "wait_for": {
                    "type": "string",
                    "description": (
                        "open/send/screen: regular expression to wait for on the screen before returning "
                        "(e.g. a prompt like '\\$ $' or the word 'Password'). Without it the call returns "
                        "when output goes quiet."
                    ),
                },
                "wait_seconds": {
                    "type": "number",
                    "description": f"Longest time to wait for wait_for or for output to settle (default {_DEFAULT_WAIT_S:g}, max {_MAX_WAIT_S:g}).",
                },
                "cols": {"type": "integer", "description": "open/resize: screen width in columns (default 120)."},
                "rows": {"type": "integer", "description": "open/resize: screen height in rows (default 40)."},
                "scrollback": {
                    "type": "integer",
                    "description": "send/screen/close: also return this many lines that scrolled off the top of the screen.",
                },
                "name": {"type": "string", "description": "open: optional short label; usable in place of the id."},
            },
            "required": ["action"],
        },
        category="core",
        tags=[
            "terminal",
            "pty",
            "tty",
            "interactive",
            "ssh",
            "repl",
            "tui",
            "screen",
            "keys",
            "keyboard",
            "prompt",
            "expect",
            "tmux",
            "console",
            "shell",
            "session",
            "game",
        ],
        timeout=int(_MAX_WAIT_S) + 30,
        parallel_safe=False,
        safety_level="dangerous",
        idempotent=False,  # the screen changes; a repeat call must never be served from cache
    )
