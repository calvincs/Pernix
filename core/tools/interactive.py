"""Shared pieces for the interactive tools (`terminal` and `browser`).

Both tools take keyboard input as one string in the same notation: plain text
with named keys in angle brackets, e.g. ``ls -la<enter>`` or
``<ctrl-c><wait:0.5>q``. One parser serves both so an agent learns it once;
each tool then maps the parsed steps to its own wire form (terminal bytes or
browser key names).

Both tools also keep a live resource (a process, a page) that outlives the
tool call, so both need the same ownership rule: the session that opened it,
its parent, its workers and its siblings may use it. A handoff between a
parent and its worker is the normal case, not an exception.
"""

from __future__ import annotations

import re

# A token is "<name>" where name is short and has no spaces or angle brackets.
# Anything that is not a KNOWN name stays literal text, so "a < b > c" and
# "<div>" pass through untouched.
_TOKEN_RE = re.compile(r"<([A-Za-z0-9:.+\-\[\]\\^_ ]{1,24})>")

_ALIASES = {
    "return": "enter",
    "cr": "enter",
    "escape": "esc",
    "bs": "backspace",
    "del": "delete",
    "ins": "insert",
    "pageup": "pgup",
    "pagedown": "pgdn",
    "pgdown": "pgdn",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "backtab": "shift-tab",
    "lt": "lt",
}

_PLAIN_KEYS = frozenset(
    {
        "enter",
        "tab",
        "esc",
        "space",
        "backspace",
        "delete",
        "insert",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pgup",
        "pgdn",
        "shift-tab",
    }
    | {f"f{n}" for n in range(1, 13)}
)

MAX_WAIT_STEP_S = 10.0


def _normalize_key(raw: str) -> tuple[str, object] | None:
    """One token body -> a step, or None when it is not a key name."""
    name = raw.strip().lower()
    name = _ALIASES.get(name, name)
    if name == "lt":
        return ("text", "<")
    if name in _PLAIN_KEYS:
        return ("key", name)
    if name.startswith("wait:"):
        try:
            secs = float(name[5:])
        except ValueError:
            return None
        return ("wait", max(0.0, min(secs, MAX_WAIT_STEP_S)))
    for prefix, canon in (("ctrl-", "ctrl-"), ("c-", "ctrl-"), ("ctrl+", "ctrl-"), ("alt-", "alt-"), ("m-", "alt-")):
        if name.startswith(prefix):
            rest = name[len(prefix) :]
            if rest == "space":
                rest = " "
            if len(rest) == 1:
                return ("key", canon + rest)
            if canon == "alt-" and (rest in _PLAIN_KEYS):
                return ("key", canon + rest)
            return None
    return None


def parse_input(text: str, raw: bool = False) -> list[tuple[str, object]]:
    """Split an input string into steps: ("text", str), ("key", name), ("wait", secs).

    With raw=True the whole string is one literal text step. Unknown
    ``<tokens>`` are kept as literal text. ``<lt>`` types a literal "<" for the
    rare case where real text would otherwise read as a key name.
    """
    if not text:
        return []
    if raw:
        return [("text", text)]
    steps: list[tuple[str, object]] = []
    pos = 0

    def _add_text(s: str) -> None:
        if not s:
            return
        if steps and steps[-1][0] == "text":
            steps[-1] = ("text", str(steps[-1][1]) + s)
        else:
            steps.append(("text", s))

    for m in _TOKEN_RE.finditer(text):
        step = _normalize_key(m.group(1))
        if step is None:
            continue  # not a key: leave it inside the surrounding literal text
        _add_text(text[pos : m.start()])
        if step[0] == "text":
            _add_text(str(step[1]))
        else:
            steps.append(step)
        pos = m.end()
    _add_text(text[pos:])
    return steps


KEY_HELP = (
    "Plain text is typed as-is; named keys go in angle brackets: <enter> <tab> <esc> <space> "
    "<backspace> <delete> <up> <down> <left> <right> <home> <end> <pgup> <pgdn> <f1>..<f12> "
    "<ctrl-c> (any ctrl-letter) <alt-x> <shift-tab>; <wait:0.5> pauses between keys; <lt> is a literal '<'."
)


# --- terminal wire form -----------------------------------------------------

_TERM_KEYS = {
    "enter": "\r",
    "tab": "\t",
    "esc": "\x1b",
    "space": " ",
    "backspace": "\x7f",
    "delete": "\x1b[3~",
    "insert": "\x1b[2~",
    "pgup": "\x1b[5~",
    "pgdn": "\x1b[6~",
    "shift-tab": "\x1b[Z",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f11": "\x1b[23~",
    "f12": "\x1b[24~",
}
_TERM_CURSOR = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}


def terminal_bytes(key: str, app_cursor: bool = False) -> bytes:
    """Bytes a real terminal sends for one named key.

    app_cursor: the program switched the terminal to application cursor mode
    (DECCKM), where arrows/home/end are sent as ESC O x instead of ESC [ x.
    Full-screen programs (vim, less, many TUIs) rely on this.
    """
    if key in _TERM_CURSOR:
        return ("\x1bO" if app_cursor else "\x1b[").encode() + _TERM_CURSOR[key].encode()
    if key in _TERM_KEYS:
        return _TERM_KEYS[key].encode()
    if key.startswith("ctrl-"):
        ch = key[5:]
        if ch == " " or ch == "@":
            return b"\x00"
        if ch == "?":
            return b"\x7f"
        code = ord(ch.upper())
        if 0x40 <= code <= 0x5F:
            return bytes([code & 0x1F])
        return ch.encode()
    if key.startswith("alt-"):
        rest = key[4:]
        if len(rest) == 1:
            return b"\x1b" + rest.encode()
        return b"\x1b" + terminal_bytes(rest, app_cursor)
    return key.encode()


# --- browser wire form ------------------------------------------------------

_BROWSER_KEYS = {
    "enter": "Enter",
    "tab": "Tab",
    "esc": "Escape",
    "space": "Space",
    "backspace": "Backspace",
    "delete": "Delete",
    "insert": "Insert",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "home": "Home",
    "end": "End",
    "pgup": "PageUp",
    "pgdn": "PageDown",
    "shift-tab": "Shift+Tab",
}


def browser_key(key: str) -> str:
    """Playwright key name for one named key."""
    if key in _BROWSER_KEYS:
        return _BROWSER_KEYS[key]
    if len(key) > 1 and key[0] == "f" and key[1:].isdigit():
        return "F" + key[1:]
    for prefix, mod in (("ctrl-", "Control+"), ("alt-", "Alt+")):
        if key.startswith(prefix):
            rest = key[len(prefix) :]
            if rest == " ":
                return mod + "Space"
            if len(rest) == 1:
                return mod + rest
            return mod + browser_key(rest)
    return key


# --- ownership --------------------------------------------------------------


def _parent_of(session_id: str) -> str:
    try:
        from db import models as db

        row = db.get_session(session_id)
        return (row or {}).get("parent_session_id") or ""
    except Exception:
        return ""


def same_family(owner: str, caller: str) -> bool:
    """True when `caller` may use a resource `owner` opened: the same session,
    parent and worker either way round, or two workers of one parent.

    Reads the sessions DB, so call it from a tool thread, never from the loop.
    """
    if not owner or not caller:
        return False
    if owner == caller:
        return True
    owner_parent = _parent_of(owner)
    if owner_parent and owner_parent == caller:
        return True
    caller_parent = _parent_of(caller)
    if caller_parent and caller_parent == owner:
        return True
    return bool(owner_parent) and owner_parent == caller_parent
