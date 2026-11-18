"""An agent was asked to play a terminal game served through a web page. The
page drew its terminal on a canvas, so browse_web returned no text; bash runs
one command and returns; job_start can be read but not typed into. With no
tool for a program or a page that wants a conversation, the agent built the
missing layer itself — a daemon to hold the connection, a socket to send keys,
an escape-code parser to rebuild the screen — spent about 35 rounds on it, and
later rewrote it four more times. The game itself took a helper minutes once
the plumbing worked.

Two tools close the gap, each one tool with an `action` argument so the fixed
tool list grows by two entries:

- `terminal`: a command on a real pseudo-terminal that stays open between
  calls, with the screen kept by an emulator and returned as plain text.
- `browser`: a page that stays open between calls, with snapshots that number
  the clickable elements, screenshots the model can see, key input for canvas
  pages, and notes about canvases and WebSockets.

Both take input in one notation (`text<enter>`, `<ctrl-c>`, `<wait:0.5>`),
and both let a parent and its workers share what either opened.
"""

import asyncio
import http.server
import os
import sys
import textwrap
import threading
import time

import pytest

from core.tools import interactive
from core.tools.builtin import terminal_tool
from core.tools.native import NATIVE_TOOLS
from db import models as db

# --- shared input notation --------------------------------------------------


def test_input_notation_splits_text_keys_and_waits():
    steps = interactive.parse_input("ls -la<enter><wait:0.5><ctrl-c>q")
    assert steps == [("text", "ls -la"), ("key", "enter"), ("wait", 0.5), ("key", "ctrl-c"), ("text", "q")]


def test_text_that_only_looks_like_a_key_stays_text():
    # HTML, comparisons and unknown names must reach the program untouched.
    assert interactive.parse_input("<div>a < b > c</div>") == [("text", "<div>a < b > c</div>")]
    assert interactive.parse_input("if x <y> z") == [("text", "if x <y> z")]
    assert interactive.parse_input("<lt>enter>") == [("text", "<enter>")]
    assert interactive.parse_input("<enter>", raw=True) == [("text", "<enter>")]


def test_named_keys_map_to_what_a_terminal_and_a_browser_send():
    assert interactive.terminal_bytes("enter") == b"\r"
    assert interactive.terminal_bytes("ctrl-c") == b"\x03"
    assert interactive.terminal_bytes("f6") == b"\x1b[17~"
    assert interactive.terminal_bytes("up") == b"\x1b[A"
    assert interactive.terminal_bytes("up", app_cursor=True) == b"\x1bOA"
    assert interactive.terminal_bytes("alt-x") == b"\x1bx"
    assert interactive.browser_key("enter") == "Enter"
    assert interactive.browser_key("f6") == "F6"
    assert interactive.browser_key("ctrl-a") == "Control+a"
    assert interactive.browser_key("pgdn") == "PageDown"


# --- terminal ---------------------------------------------------------------


@pytest.fixture
def term():
    """A session plus a caller; every terminal it opened is closed afterwards."""
    sid = db.create_session(title="terminal test")
    ctx = {"session_id": sid}

    def call(**kwargs):
        return terminal_tool.terminal(_context=ctx, **kwargs)

    call.ctx = ctx
    call.sid = sid
    yield call
    terminal_tool.close_all()


def _id_of(result: str) -> str:
    assert result.startswith("[terminal t-"), result
    return result.split()[1]


def _alive(pid: int) -> bool:
    try:
        state = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def test_a_program_stays_open_between_calls_and_shows_its_screen(term):
    opened = term(action="open", command="bash --norc --noprofile -i", cols=80, rows=12)
    assert "running pid" in opened and "80x12" in opened
    tid = _id_of(opened)

    out = term(action="send", id=tid, input="X=41; echo value $((X+1))<enter>")
    assert "value 42" in out
    # The shell variable is still there on the next call: one live process.
    out = term(action="send", id=tid, input="echo again $X<enter>")
    assert "again 41" in out
    assert "running pid" in term(action="screen", id=tid)


def test_wait_for_sees_a_prompt_that_ends_in_a_space_and_ignores_the_old_one(term):
    term(action="open", command="bash --norc --noprofile -i", cols=80, rows=12)
    out = term(action="send", input=f"{sys.executable} -q<enter>", wait_for=r">>> $", wait_seconds=20)
    assert "wait_for matched" in out, out
    # The prompt is already on screen before the program answers. A wait that
    # accepted it would return without the result of the slow statement.
    out = term(
        action="send",
        input="import time; time.sleep(0.8); print('slow' + 'answer')<enter>",
        wait_for=r">>> $",
        wait_seconds=20,
    )
    assert "slowanswer" in out, out


def test_a_full_screen_program_is_read_as_a_screen_not_a_byte_log(term, tmp_path):
    # Paints at fixed positions, asks the terminal where the cursor is, turns
    # on application cursor keys, then names the key it receives. Each part
    # is something a hand-rolled parser got wrong.
    script = tmp_path / "tui.py"
    script.write_text(textwrap.dedent(r"""
            import os, sys, termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
            try:
                w = lambda s: (sys.stdout.write(s), sys.stdout.flush())
                w("\x1b[2J\x1b[3;10HSTATUS: READY\x1b[5;1Hold text that gets replaced")
                w("\x1b[5;1H\x1b[2Kline five\x1b[7;4H\x1b[6n")
                reply = b""
                while not reply.endswith(b"R"):
                    reply += os.read(fd, 1)
                w("\x1b[8;1Hcursor report " + reply[2:].decode())
                w("\x1b[?1h")
                key = os.read(fd, 8)
                w("\x1b[9;1Hkey " + repr(key))
                os.read(fd, 1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            """))
    out = term(action="open", command=f"{sys.executable} {script}", cols=60, rows=12, wait_for="cursor report")
    lines = out.split("\n")
    screen = lines[2:]  # header, note, then the screen
    assert screen[2] == " " * 9 + "STATUS: READY"
    assert screen[4] == "line five"  # the erased text is gone, not appended
    assert "cursor report 7;4R" in out  # the emulator answered the program's question
    out = term(action="send", input="<up>", wait_for="key ")
    assert r"key b'\x1bOA'" in out  # application cursor mode honoured
    out = term(action="send", input="q", wait_seconds=3)
    assert "exited code 0" in term(action="screen")


def test_scrollback_keeps_what_left_the_screen(term):
    term(action="open", command="bash --norc --noprofile -i", cols=80, rows=8)
    out = term(action="send", input="seq 1 30<enter>")
    assert "scrolled off the top" in out and "\n7\n" not in out
    out = term(action="screen", scrollback=40)
    assert "\n7\n" in out and "--- screen ---" in out


def test_ctrl_c_reaches_the_foreground_program(term):
    term(action="open", command="bash --norc --noprofile -i", cols=80, rows=10)
    out = term(action="send", input="sleep 600<enter><wait:0.5><ctrl-c>echo back-at-prompt<enter>")
    assert "back-at-prompt\n" in out


def test_close_stops_everything_the_terminal_started(term, tmp_path):
    pidfile = tmp_path / "child.pid"
    term(action="open", command="bash --norc --noprofile -i", cols=80, rows=10)
    term(action="send", input=f"(sleep 600 & echo $! > {pidfile}; wait)<enter>", wait_seconds=1)
    deadline = time.time() + 5
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    child = int(pidfile.read_text())
    assert _alive(child)
    out = term(action="close")
    assert "closed by the agent" in out
    assert not _alive(child)
    assert "No terminals are open" in term(action="list")


def test_input_to_a_finished_program_is_reported_not_swallowed(term):
    term(action="open", command="echo bye", wait_seconds=3)
    time.sleep(1.5)  # the reader notices the exit on its next quiet second
    out = term(action="send", input="hello<enter>")
    assert "input was NOT sent" in out and "bye" in out


def test_the_shell_policy_follows_the_command_into_the_terminal(term, monkeypatch):
    monkeypatch.setattr("config.settings.shell_security_mode", "strict")
    monkeypatch.setattr("config.settings.shell_allowlist", ["echo"])
    out = term(action="open", command="definitely-not-allowed --flag")
    assert out.startswith("Error: Command 'definitely-not-allowed' not in allowlist")
    assert term(action="open", command="   ").startswith("Error")


def test_per_session_cap_is_enforced(term, monkeypatch):
    monkeypatch.setattr("config.settings.terminal_max_per_session", 1)
    term(action="open", command="sleep 60", wait_seconds=0.5)
    assert "already open" in term(action="open", command="sleep 60", wait_seconds=0.5)


def test_a_worker_can_take_over_its_parents_terminal_and_a_stranger_cannot(term):
    tid = _id_of(term(action="open", command="bash --norc --noprofile -i", cols=80, rows=10))
    worker = db.create_session(title="worker", session_type="worker", parent_session_id=term.sid)
    stranger = db.create_session(title="someone else")

    out = terminal_tool.terminal(action="send", input="echo from-worker<enter>", _context={"session_id": worker})
    assert "from-worker\n" in out
    out = terminal_tool.terminal(action="screen", id=tid, _context={"session_id": stranger})
    assert out == f"Error: terminal {tid} belongs to another session."
    assert "No terminals are open" in terminal_tool.terminal(action="list", _context={"session_id": stranger})


def test_deleting_the_session_ends_its_terminals(term):
    opened = term(action="open", command="sleep 600", wait_seconds=0.5)
    pid = int(opened.split("running pid ")[1].split()[0])
    assert _alive(pid)
    assert terminal_tool.close_for_session(term.sid) == 1
    assert not _alive(pid)


def test_an_idle_terminal_is_closed_by_its_own_clock(term, monkeypatch):
    monkeypatch.setattr("config.settings.terminal_idle_timeout_s", 1)
    opened = term(action="open", command="sleep 600", wait_seconds=0.5)
    pid = int(opened.split("running pid ")[1].split()[0])
    deadline = time.time() + 10
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not _alive(pid)
    with terminal_tool._registry_lock:
        t = next(iter(terminal_tool._terminals.values()))
    assert "without a tool call" in t.end_reason


# --- wiring -----------------------------------------------------------------


def test_both_tools_are_in_the_fixed_list_and_the_worker_kinds():
    from core.extensions.orchestration.kinds import _BUILTIN_KINDS

    assert {"terminal", "browser"} <= NATIVE_TOOLS
    for kind in ("code", "debug", "transform"):
        assert "terminal" in _BUILTIN_KINDS[kind].tool_allowlist, kind
    assert "browser" in _BUILTIN_KINDS["research"].tool_allowlist


def test_scout_is_told_to_name_the_interactive_tools():
    from core.scout.runner import _scout_system_prompt

    prompt = _scout_system_prompt()
    assert "terminal tool" in prompt and "browser tool" in prompt
    # The no-think line must stay last; conditional rules go in above it.
    assert prompt.index("INTERACTIVE PROGRAMS AND PAGES") < prompt.rindex("Do NOT use <think>")


# --- browser ----------------------------------------------------------------

PAGE = """<!doctype html><html><head><title>Test Bench</title></head><body>
<h1>Bench</h1>
<p id="status">nothing yet</p>
<input id="name" placeholder="Your name">
<button id="go" onclick="document.getElementById('status').textContent='hello ' + document.getElementById('name').value">Greet</button>
<select id="pick" aria-label="Colour"><option value="r">Red</option><option value="g">Green</option></select>
<a href="/second">Second page</a>
<canvas id="c" width="300" height="120" tabindex="0" style="display:block"></canvas>
<p id="keys"></p>
<script>
const c = document.getElementById('c'), g = c.getContext('2d');
g.fillStyle = '#204080'; g.fillRect(0, 0, 300, 120);
const log = document.getElementById('keys');
c.addEventListener('keydown', (e) => { log.textContent += '[' + e.key + ']'; });
c.addEventListener('click', (e) => { log.textContent += '(click ' + Math.round(e.offsetX) + ')'; });
</script></body></html>"""

SECOND = "<!doctype html><html><head><title>Second</title></head><body><p>second page body</p></body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (SECOND if self.path.startswith("/second") else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def web(monkeypatch):
    """A local page, an event loop on its own thread (the tool marshals onto
    it, as it does onto the server's), and a caller bound to one session."""
    pytest.importorskip("playwright")
    from core.extensions import web as web_ext
    from core.extensions.web import browser_session

    monkeypatch.setattr("config.settings.network_enabled", False)  # loopback pages allowed
    monkeypatch.setattr("config.settings.browser_enabled", True)
    monkeypatch.setattr("config.settings.browser_headless", True)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    web_ext._browser_init_lock = None  # the lock belongs to whichever loop made it

    sid = db.create_session(title="browser test")
    ctx = {"session_id": sid, "_loop": loop}

    def call(**kwargs):
        return browser_session.browser(_context=ctx, **kwargs)

    call.ctx = ctx
    call.sid = sid
    call.loop = loop
    call.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        probe = call(action="open", url=call.base)
        if "Chromium not installed" in probe or "Executable doesn't exist" in probe:
            pytest.skip("Chromium is not installed for Playwright")
        call.first = probe
        yield call
    finally:
        asyncio.run_coroutine_threadsafe(web_ext._close_browser(), loop).result(timeout=20)
        web_ext._browser_init_lock = None
        loop.call_soon_threadsafe(loop.stop)
        server.shutdown()


def _ref(snapshot: str, needle: str) -> str:
    for line in snapshot.split("\n"):
        if line.startswith("[") and needle in line and "]" in line[:5]:
            return line.split("]")[0] + "]"
    raise AssertionError(f"no element matching {needle!r} in:\n{snapshot}")


def test_a_page_stays_open_and_can_be_operated(web):
    snap = web.first
    assert "Test Bench" in snap.split("\n")[0] and "nothing yet" in snap
    assert 'input:text "Your name"' in snap and 'button "Greet"' in snap

    web(action="type", target=_ref(snap, "Your name"), input="Ada")
    out = web(action="click", target=_ref(snap, "Greet"))
    assert "hello Ada" in out  # same page, state kept between three calls

    out = web(action="select", target="#pick", input="Green")
    assert 'value="Green"' in out or "Green" in web(
        action="eval", script="document.getElementById('pick').selectedOptions[0].label"
    )

    out = web(action="click", target="text=Second page")
    assert "second page body" in out and "/second" in out.split("\n")[0]
    assert "Bench" in web(action="back")


def test_a_canvas_page_is_flagged_and_can_be_driven_by_keys_and_pixels(web):
    snap = web.first
    assert "canvas 300x120" in snap and "NOT in the text above" in snap
    out = web(action="click", target="#c")
    assert "(click " in out
    out = web(action="type", input="a<f6><enter><up>")
    assert "[a][F6][Enter][ArrowUp]" in out
    box = web(
        action="eval",
        script="(() => { const r = document.getElementById('c').getBoundingClientRect(); return [r.left, r.top]; })()",
    )
    left, top = [float(v) for v in box.split("\n", 1)[1].strip("[] \n").replace("\n", "").split(",")]
    out = web(action="click", x=left + 40, y=top + 10)
    assert "(click 40)" in out


def test_websocket_traffic_shows_up_in_the_snapshot_notes(web):
    # A page that talks over a WebSocket looks empty to a text extractor. The
    # snapshot has to say the socket exists and whether frames are flowing.
    websockets = pytest.importorskip("websockets")
    from websockets.sync.server import serve

    def echo(conn):
        for message in conn:
            conn.send("echo:" + message)

    with serve(echo, "127.0.0.1", 0) as ws_server:
        threading.Thread(target=ws_server.serve_forever, daemon=True).start()
        port = ws_server.socket.getsockname()[1]
        script = (
            "() => new Promise((resolve) => { const ws = new WebSocket('ws://127.0.0.1:%d/');"
            " ws.onopen = () => ws.send('ping'); ws.onmessage = (e) => resolve(e.data); })" % port
        )
        assert "echo:ping" in web(action="eval", script=script)
        snap = web(action="snapshot")
        ws_server.shutdown()
    line = next(ln for ln in snap.split("\n") if "websocket ws://127.0.0.1" in ln)
    assert "1 frames in, 1 out" in line


def test_a_screenshot_is_saved_and_put_in_front_of_the_model(web):
    out = web(action="screenshot")
    path = out.split("Screenshot saved: ")[1].split(" (viewport")[0]
    assert os.path.getsize(path) > 1000
    assert "Image queued" in out
    rows = [m for m in db.get_messages(web.sid) if "[view_image]" in (m.get("content") or "")]
    assert rows and path in rows[-1]["content"]


def test_eval_returns_data_and_errors_come_back_as_text(web):
    assert '"Test Bench"' in web(action="eval", script="document.title")
    out = web(action="eval", script="() => [...document.querySelectorAll('option')].map(o => o.value)")
    assert '"r"' in out and '"g"' in out
    out = web(action="click", target="#does-not-exist")
    assert out.startswith("Error: click failed") and "fresh snapshot" in out


def test_private_addresses_and_odd_schemes_are_refused(web, monkeypatch):
    assert web(action="goto", url="file:///etc/passwd").startswith("Error: Blocked URL scheme")
    monkeypatch.setattr("config.settings.network_enabled", True)  # shared host: loopback is off limits
    out = web(action="goto", url="http://127.0.0.1:9/")
    assert out.startswith("Error: Blocked") and "private/internal" in out


def test_pages_are_shared_with_workers_capped_and_closed(web, monkeypatch):
    from core.extensions.web import browser_session

    worker = db.create_session(title="worker", session_type="worker", parent_session_id=web.sid)
    stranger = db.create_session(title="someone else")
    out = browser_session.browser(action="snapshot", _context={"session_id": worker, "_loop": web.loop})
    assert "Test Bench" in out
    out = browser_session.browser(action="snapshot", _context={"session_id": stranger, "_loop": web.loop})
    assert out.startswith("Error: no page is open")

    monkeypatch.setattr("config.settings.browser_max_sessions", 1)
    assert "already open" in web(action="open", url=web.base)
    assert "closed" in web(action="close")
    assert "No pages are open" in web(action="list")
