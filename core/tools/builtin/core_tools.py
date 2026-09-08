"""Pernix — Core tools: file_read, file_write, bash."""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from config import settings
from core.tools.atomic import TargetBusy, atomic_write, file_revision, target_lock
from core.tools.paths import (
    PROTECTED_DIRS,
    PROTECTED_FILES,
    root_mismatch_hint,
)
from core.tools.paths import (
    allowed_read_roots as _allowed_roots,
)
from core.tools.paths import (
    build_shell_env as _build_shell_env,
)
from core.tools.paths import (
    safe_read_path as _safe_path,
)
from core.tools.paths import (
    safe_write_path as _safe_write_path,
)
from core.tools.paths import (
    workspace as _workspace,
)
from core.tools.paths import (
    workspace_home as _workspace_home,
)
from core.tools.truncation import MAX_OUTPUT, truncate_output

# Upper bound on a single file_write payload (mirrors bash RLIMIT_FSIZE).
MAX_WRITE_SIZE = 100 * 1024 * 1024

# Prefix commands that wrap another command without changing its effect for
# our security purposes — we peel them off before classifying the "real"
# command word. e.g. `env VAR=x sudo rm -rf /` → we must see `sudo` and `rm`.
PREFIX_WRAPPERS = frozenset(
    {
        "env",
        "nice",
        "nohup",
        "time",
        "ionice",
        "chrt",
        "taskset",
        "stdbuf",
        "unbuffer",
        "exec",
        "sudo",
        "doas",
    }
)

# Wrappers whose short flags take a value token (e.g. `nice -n 10 cmd`).
WRAPPER_FLAGS_WITH_VALUE = {
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p", "-P", "-u"},
    "chrt": {"-p"},
    "taskset": {"-c", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "sudo": {"-u", "-g", "-U", "-p", "-C", "-r", "-t"},
    "doas": {"-u", "-C"},
    "time": {"-f", "-o"},
}

# Shells whose `-c SCRIPT` payload we recurse into for inspection.
SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "ksh", "dash", "ash"})

logger = logging.getLogger("pernix.tools.core")

# Shell denylist patterns (defense-in-depth, not security boundary)
SHELL_DENYLIST = [
    re.compile(r"rm\s+.*-[a-zA-Z]*r[a-zA-Z]*f"),
    re.compile(r"rm\s+.*-[a-zA-Z]*f[a-zA-Z]*\s+/"),
    re.compile(r"dd\s+if="),
    re.compile(r"mkfs"),
    re.compile(r"shutdown|reboot|halt|poweroff"),
    re.compile(r":\(\)\s*\{"),
    re.compile(r"curl.*\|\s*(?:ba|da|z|k)?sh\b"),
    re.compile(r"wget.*\|\s*(?:ba|da|z|k)?sh\b"),
    re.compile(r"chmod\s+777\s+/"),
    re.compile(r">\s*/dev/sd"),
    re.compile(r"sudo\s+"),
    re.compile(r">\s*(?:[\w./\-]*/)?(?:AGENTS|INSTRUCTIONS|SOUL|RULES|SAFETY)\.md"),
    re.compile(r"tee\s+.*(?:[\w./\-]*/)?(?:AGENTS|INSTRUCTIONS|SOUL|RULES|SAFETY)\.md"),
    re.compile(r"cp\s+.*\s+(?:[\w./\-]*/)?(?:AGENTS|INSTRUCTIONS|SOUL|RULES|SAFETY)\.md"),
    re.compile(r"mv\s+.*\s+(?:[\w./\-]*/)?(?:AGENTS|INSTRUCTIONS|SOUL|RULES|SAFETY)\.md"),
    re.compile(r"crontab\s+"),
    re.compile(r"systemctl\s+"),
    re.compile(r"iptables\s+"),
    re.compile(r"mount\s+"),
    re.compile(r"chown\s+root"),
    re.compile(r">\s*/etc/"),
    # exec( alone is routine compute (approved narrowing, 2026-08-25 —
    # the broad form blocked a legit ARC solver run); only the
    # obfuscated-payload shape stays blocked.
    re.compile(r"python3?\s+-c\s+(?=.*exec\()(?=.*(?:base64|b64decode|fromhex|\\\\x[0-9a-f]{2}))"),
    re.compile(r"--break-system-packages"),
    re.compile(r"pip3?\s+install\s+.*--target\s+/"),
    re.compile(r"pip3?\s+install\s+.*--prefix\s+/"),
]

# Commands blocked outright in permissive mode
COMMAND_DENYLIST = frozenset(
    {
        "dd",
        "mkfs",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "crontab",
        "systemctl",
        "iptables",
        "mount",
        "umount",
    }
)

# Redirect/pipe patterns that regex catches better than tokenization
REDIRECT_DENYLIST = [
    re.compile(r">\s*/dev/sd"),
    re.compile(r">\s*/etc/"),
    re.compile(r"curl.*\|\s*(?:ba|da|z|k)?sh\b"),
    re.compile(r"wget.*\|\s*(?:ba|da|z|k)?sh\b"),
    re.compile(r":\(\)\s*\{"),  # fork bomb
    # exec( alone is routine compute (approved narrowing, 2026-08-25 —
    # the broad form blocked a legit ARC solver run); only the
    # obfuscated-payload shape stays blocked.
    re.compile(r"python3?\s+-c\s+(?=.*exec\()(?=.*(?:base64|b64decode|fromhex|\\\\x[0-9a-f]{2}))"),
]


def _extract_command_words(command: str, _depth: int = 0) -> list[str]:
    """Extract command words from a shell command using shlex tokenization.

    - Splits on pipe/chain operators and returns the first real command word
      of each segment.
    - Peels off prefix wrappers (env, nice, nohup, time, xargs, ...) so the
      actual target command is still inspected (e.g. `env sudo rm` → sudo, rm).
    - Recurses into `sh -c SCRIPT` / `bash -c SCRIPT` payloads so commands
      hidden inside a shell wrapper are still detected.
    - Falls back to whitespace splitting if shlex can't parse.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        segments = re.split(r"\s*(?:\|\||&&|;|\|)\s*", command)
        return [s.strip().split()[0].lower() for s in segments if s.strip()]

    commands: list[str] = []
    i = 0
    expect_command = True
    # Guard against deeply nested shell -c to avoid pathological recursion.
    max_depth = 3

    while i < len(tokens):
        token = tokens[i]
        if token in ("|", "||", "&&", ";"):
            expect_command = True
            i += 1
            continue

        if expect_command:
            # Strip leading env-var assignments like VAR=val.
            if "=" in token and not token.startswith("-") and not token.startswith("/"):
                # Only treat as env assignment if LHS looks like a var name.
                lhs = token.split("=", 1)[0]
                if lhs and (lhs[0].isalpha() or lhs[0] == "_") and all(c.isalnum() or c == "_" for c in lhs):
                    i += 1
                    continue

            base = os.path.basename(token).lower()

            # Peel prefix wrappers: keep scanning to find the actual command.
            if base in PREFIX_WRAPPERS:
                commands.append(base)
                i += 1
                flags_with_value = WRAPPER_FLAGS_WITH_VALUE.get(base, set())
                # Skip the wrapper's own flags (and their value tokens).
                while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-":
                    flag = tokens[i]
                    i += 1
                    # If the flag has an explicit "=value", value is attached.
                    # Otherwise, if flag is known to take a value, eat the next token.
                    if "=" not in flag and flag in flags_with_value and i < len(tokens):
                        i += 1
                # For env: skip VAR=val assignments until we see the real command.
                if base == "env":
                    while i < len(tokens):
                        t = tokens[i]
                        if "=" in t and not t.startswith("-") and not t.startswith("/"):
                            lhs = t.split("=", 1)[0]
                            if (
                                lhs
                                and (lhs[0].isalpha() or lhs[0] == "_")
                                and all(c.isalnum() or c == "_" for c in lhs)
                            ):
                                i += 1
                                continue
                        break
                # expect_command stays True so the wrapped command is inspected.
                continue

            # xargs: next non-flag token is the wrapped command.
            if base == "xargs":
                commands.append(base)
                i += 1
                while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-":
                    i += 1
                continue

            commands.append(base)

            # Recurse into shell -c SCRIPT payloads.
            if base in SHELL_WRAPPERS and _depth < max_depth:
                # Look ahead for -c followed by a script string.
                j = i + 1
                while j < len(tokens) and tokens[j].startswith("-"):
                    if tokens[j] == "-c" and j + 1 < len(tokens):
                        commands.extend(_extract_command_words(tokens[j + 1], _depth + 1))
                        break
                    j += 1

            expect_command = False
        i += 1

    return commands


# Cache-only directory names whose `rm -rf` is treated as safe even though the
# `-rf` flag combination would otherwise trigger the recursive-force denylist.
# These are routinely deleted during Python development (cache invalidation,
# fresh-import scenarios) and refusing them forces the agent into rm-rf
# gymnastics that look like the agent ignoring instructions. Every entry here
# is a final path segment, not a prefix — `/foo/__pycache__` matches but
# `/__pycache__/lib` does NOT (we won't allow descending recursively into a
# path that merely contains the cache name).
_SAFE_CACHE_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        "node_modules",  # routine in JS dev; same fresh-deps story
    }
)


def _split_shell_operators(command: str) -> list[str]:
    """Split a command string on shell operators, respecting quotes.

    shlex alone is not enough: it keeps `x;` as one token, so a `;` written
    without a leading space hides the next command word. This scans the raw
    string instead, tracking quote state and backslash escapes, and cuts at
    `;` `&&` `||` `|` `&` and newlines that are not inside quotes.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch in ";\n":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        if ch in "&|":
            parts.append("".join(buf))
            buf = []
            i += 2 if i + 1 < n and command[i + 1] == ch else 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p for p in (p.strip() for p in parts) if p]


# Tokens that are shell plumbing, not arguments: redirections and fd dups.
_REDIRECT_RE = re.compile(r"^\d*(?:>>?|<<?|>&|<&|&>)")


def _strip_redirections(tokens: list[str]) -> list[str]:
    """Drop redirection tokens (and a following target) from an argument list.

    `rm -rf x 2>/dev/null` must be read as one target, not two — otherwise the
    exemption checks below see `2>/dev/null` as a path outside the workspace
    and refuse a command that only touches the workspace.
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _REDIRECT_RE.match(tok):
            # A bare operator takes the next token as its target.
            if _REDIRECT_RE.fullmatch(tok):
                i += 2
            else:
                i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _rm_segments(command: str) -> list[list[str]] | None:
    """Every `rm` invocation in a command, as its own token list.

    A command is rarely a bare `rm`. `cd impl && rm -rf __pycache__` and
    `mv a b 2>/dev/null && rm -rf tmpdir; ls` are the ordinary shapes, and both
    used to skip the exemption checks below entirely because those checks began
    with `tokens[0] != "rm"` — so the agent was refused by an error message
    that told it the very target it had just used was allowed (Agent Mesh
    build, 2026-09-08).

    Returns None when a segment cannot be tokenized — the caller keeps the
    block, because an unparseable command is not one we can clear.
    """
    out: list[list[str]] = []
    for segment in _split_shell_operators(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return None
        i = 0
        # Peel env assignments and prefix wrappers (env/nice/sudo/...) so
        # `env FOO=1 rm -rf x` is still seen as an rm.
        while i < len(tokens):
            tok = tokens[i]
            if "=" in tok and not tok.startswith(("-", "/")):
                lhs = tok.split("=", 1)[0]
                if lhs and (lhs[0].isalpha() or lhs[0] == "_") and all(c.isalnum() or c == "_" for c in lhs):
                    i += 1
                    continue
            base = os.path.basename(tok).lower()
            if base in PREFIX_WRAPPERS:
                i += 1
                while i < len(tokens) and tokens[i].startswith("-"):
                    takes_value = tokens[i] in WRAPPER_FLAGS_WITH_VALUE.get(base, set())
                    i += 1
                    if takes_value and i < len(tokens):
                        i += 1
                continue
            break
        if i < len(tokens) and os.path.basename(tokens[i]).lower() == "rm":
            out.append(["rm"] + _strip_redirections(tokens[i + 1 :]))
    return out


def _rm_targets_are_safe_caches(command: str) -> bool:
    """Return True iff every non-flag argument to `rm` is a cache directory.

    Used to permit `rm -rf __pycache__` and similar without lifting the broader
    `rm -rf` block. Conservative: any non-cache target (file path, absolute
    path, glob, env var, parent-traversal, multiple tokens with one unsafe)
    fails the check and keeps the original block in place.

    EVERY rm in the command must qualify — a compound command that cleans a
    cache and also deletes something else stays blocked.
    """
    segments = _rm_segments(command)
    if not segments:
        return False  # parse failure or no rm at all → don't take the safe path
    for tokens in segments:
        targets = [tok for tok in tokens[1:] if not tok.startswith("-")]
        if not targets:
            return False
        for t in targets:
            # Reject absolute paths, parent traversal, env interpolation, globs.
            if t.startswith("/") or ".." in t.split("/") or "$" in t or "*" in t or "?" in t:
                return False
            # The final path segment must match a known cache directory name.
            last = t.rstrip("/").rsplit("/", 1)[-1]
            if last not in _SAFE_CACHE_DIRS:
                return False
    return True


def _collapse_repeated_lines(output: str, threshold: int = 6, keep: int = 3) -> str:
    """Collapse runs of identical lines that appear `threshold`+ times.

    ARC-3 sweep: library banners (e.g. 54 identical 'Got anonymous API key'
    INFO lines in one session) drowned the agent's own solver output — one
    agent said so verbatim. Keeps the first `keep` occurrences of any line
    repeated threshold+ times and replaces the rest with a count marker.
    Order-preserving; only exact duplicates collapse."""
    lines = output.split("\n")
    if len(lines) < threshold:
        return output
    from collections import Counter

    counts = Counter(line for line in lines if line.strip())
    noisy = {line for line, c in counts.items() if c >= threshold}
    if not noisy:
        return output
    out: list[str] = []
    seen: dict[str, int] = {}
    omitted: dict[str, int] = {}
    for line in lines:
        if line in noisy:
            seen[line] = seen.get(line, 0) + 1
            if seen[line] > keep:
                omitted[line] = omitted.get(line, 0) + 1
                continue
        out.append(line)
    for line, n in omitted.items():
        out.append(f"[{n} more identical lines omitted: {line[:80]!r}]")
    return "\n".join(out)


def _rm_targets_are_in_workspace(command: str) -> bool:
    """True iff every non-flag `rm` target clearly resolves inside the agent
    workspace. Approved exception (Calvin, 2026-08-25): the workspace is the
    agent's own scratch tree — refusing `rm -rf arc3/old_solvers` there forced
    error-prone workarounds (field case 8d411d30d12d). Conservative: env
    interpolation, parent traversal, and glob-leading targets all fail the
    check; a glob later in the path is allowed because expansion happens with
    cwd=workspace and the literal prefix already pins the tree.

    EVERY rm in the command must qualify, so `rm -rf ok && rm -rf /etc` stays
    blocked on the second segment."""
    from core.tools.paths import workspace

    segments = _rm_segments(command)
    if not segments:
        return False
    ws = workspace()
    for tokens in segments:
        targets = [t for t in tokens[1:] if not t.startswith("-")]
        if not targets:
            return False
        for t in targets:
            if "$" in t or ".." in t.split("/"):
                return False
            first_seg = t.lstrip("/").split("/", 1)[0]
            if any(ch in first_seg for ch in "*?["):
                return False  # `rm -rf *` — too broad even inside the workspace
            literal = t.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
            base = Path(literal) if literal.startswith("/") else ws / literal
            try:
                resolved = base.resolve()
            except OSError:
                return False
            if not (resolved.is_relative_to(ws) and resolved != ws):
                return False
    return True


def _check_command_security(command: str) -> str | None:
    """Check command against security rules. Returns error string or None if OK.

    Uses shlex tokenization for command-word extraction (handles quoting/escapes),
    plus regex fallback for redirect/pipe patterns that tokenization can't catch.

    Error messages are intentionally specific — they name the matched rule so
    the agent can pick a non-blocked alternative on its own. Returning a
    generic "blocked" message historically caused the agent to retry the same
    pattern repeatedly because it could not tell which token was the problem.
    """
    # Layer 1: shlex-based command word extraction
    cmd_words = _extract_command_words(command)
    for word in cmd_words:
        if word in COMMAND_DENYLIST:
            return (
                f"Error: Command blocked by security policy: '{word}' is "
                f"in the command denylist (system-altering: dd, mkfs, "
                f"shutdown, crontab, systemctl, mount, etc.)."
            )
        if word == "sudo":
            return (
                "Error: Command blocked by security policy: 'sudo' is "
                "denylisted — the agent runs as the user already and may "
                "not escalate privileges."
            )
        if word == "rm":
            # Flag extraction over the entire command string so it catches
            # `rm -rf` hidden inside `sh -c "..."` or similar wrappers.
            has_rf = bool(re.search(r"\brm\s+(?:\S+\s+)*-[^\s]*r[^\s]*f", command)) or bool(
                re.search(r"\brm\s+(?:\S+\s+)*-[^\s]*f[^\s]*r", command)
            )
            if not has_rf:
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    tokens = command.split()
                flags = [t for t in tokens if t.startswith("-") and t != "-"]
                all_flags = "".join(f.lstrip("-") for f in flags)
                has_rf = "r" in all_flags and "f" in all_flags
            if has_rf:
                # Allow `rm -rf` for a curated set of safe cache directories
                # (Python __pycache__, pytest/mypy/ruff caches, node_modules).
                # These are deleted routinely during normal development and
                # have no system-level consequence.
                if _rm_targets_are_safe_caches(command):
                    return None
                if _rm_targets_are_in_workspace(command):
                    return None
                return (
                    "Error: Command blocked by security policy: 'rm -rf' is "
                    "denylisted because it can recursively destroy files. "
                    "Allowed exceptions: paths inside the agent workspace "
                    "(data/workspace), or 'rm -rf <cache>' where <cache> is one "
                    "of " + ", ".join(sorted(_SAFE_CACHE_DIRS)) + ". For other "
                    "targets, delete files individually (e.g. 'rm file1 file2') "
                    "or use 'find ... -delete' for narrowly-scoped cleanup."
                )
        if word == "chmod" and "777" in command and "/" in command:
            return "Error: Command blocked by security policy: 'chmod 777' " "on system paths is denylisted."
        if word == "chown" and "root" in command:
            return "Error: Command blocked by security policy: 'chown root' " "is denylisted."

    # Layer 2: Regex for redirect/pipe patterns shlex can't catch
    normalized = " ".join(command.split()).lower()
    for pattern in REDIRECT_DENYLIST:
        if pattern.search(normalized):
            return (
                "Error: Command blocked by security policy: matched "
                "redirect/pipe denylist (writes to /etc, /dev/sd*, "
                "curl|sh / wget|sh, fork bombs, obfuscated python -c payloads). "
                f"Pattern: {pattern.pattern!r}"
            )

    # Layer 3: Protected file writes via original denylist patterns
    for pattern in SHELL_DENYLIST:
        if any(kw in pattern.pattern for kw in ("AGENTS", "INSTRUCTIONS", "SOUL", "RULES", "SAFETY")):
            if pattern.search(normalized):
                return (
                    "Error: Command blocked by security policy: writes "
                    "to protected file (AGENTS/INSTRUCTIONS/SOUL/RULES/"
                    "SAFETY .md) are denylisted."
                )

    return None


def check_shell_command(command: str) -> str | None:
    """Admission every shell launcher shares. Returns an error string or None.

    This is the whole of `bash`'s command policy, lifted out so a second
    launcher cannot become the way around it. `job_start` runs the same shell
    with the same environment and a much longer leash, so admitting there what
    bash refuses would make the policy advisory. The wording is the error the
    agent already knows, so a refusal reads the same whichever tool it used.

    The two modes differ deliberately: permissive runs the denylist scan,
    strict is a first-word allowlist. `core.gates.check_gate_command` is the
    third launcher and takes only the denylist half, for the reason documented
    there.
    """
    if not command or not command.strip():
        return "Error: Empty command"
    if settings.shell_security_mode == "permissive":
        return _check_command_security(command)
    if settings.shell_security_mode == "strict":
        first_word = command.strip().split()[0]
        if first_word not in settings.shell_allowlist:
            return f"Error: Command '{first_word}' not in allowlist. Allowed: {', '.join(sorted(settings.shell_allowlist)[:10])}..."
    return None


def _is_binary(resolved: Path) -> bool:
    """Check if file is binary by sampling first 512 bytes for null bytes."""
    try:
        with open(resolved, "rb") as f:
            chunk = f.read(512)
        return b"\x00" in chunk
    except Exception:
        return False


def _open_nofollow(resolved: Path, mode: str = "r"):
    """Open resolved path with O_NOFOLLOW, so a symlink swapped in after path
    resolution but before open is rejected rather than followed.

    Falls back to a regular open on platforms that lack O_NOFOLLOW.
    """
    flag = getattr(os, "O_NOFOLLOW", 0)
    if mode == "rb":
        fd = os.open(str(resolved), os.O_RDONLY | flag)
        return os.fdopen(fd, "rb")
    fd = os.open(str(resolved), os.O_RDONLY | flag)
    return os.fdopen(fd, "r", errors="replace")


def _read_text_nofollow(resolved: Path) -> str:
    """Read a file's text content, refusing to follow a symlink at the leaf."""
    with _open_nofollow(resolved, "r") as f:
        return f.read()


def file_read(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read a file from the workspace.

    Args:
        path: Relative path within workspace.
        offset: Starting line number (0-based). Default 0 (start of file).
        limit: Max lines to return. Default 0 (all lines, subject to size cap).
    """
    try:
        offset = int(offset) if offset else 0
        limit = int(limit) if limit else 0
        resolved = _safe_path(path)
        if not resolved.exists():
            # If it's a directory, list contents
            p = Path(path)
            for root in _allowed_roots():
                candidate = (root / path).resolve()
                if candidate.is_dir() and candidate.is_relative_to(root):
                    entries = sorted(candidate.iterdir(), key=lambda e: (not e.is_dir(), e.name))
                    lines = []
                    for e in entries[:200]:
                        prefix = "d " if e.is_dir() else "  "
                        lines.append(f"{prefix}{e.name}")
                    result = "\n".join(lines)
                    if len(entries) > 200:
                        result += f"\n[... {len(entries) - 200} more entries]"
                    return result
            return f"Error: File not found: {path}{root_mismatch_hint(path)}"
        if resolved.is_dir():
            entries = sorted(resolved.iterdir(), key=lambda e: (not e.is_dir(), e.name))
            lines = []
            for e in entries[:200]:
                prefix = "d " if e.is_dir() else "  "
                lines.append(f"{prefix}{e.name}")
            result = "\n".join(lines)
            if len(entries) > 200:
                result += f"\n[... {len(entries) - 200} more entries]"
            return result
        if not resolved.is_file():
            return f"Error: Not a file: {path}"
        if _is_binary(resolved):
            size = resolved.stat().st_size
            return f"Error: Binary file ({size} bytes). Use bash to inspect binary files."

        # Line-based reading with offset/limit
        if offset > 0 or limit > 0:
            lines = []
            total_lines = 0
            total_chars = 0
            with _open_nofollow(resolved, "r") as f:
                for i, line in enumerate(f):
                    total_lines = i + 1
                    if i < offset:
                        continue
                    if limit > 0 and len(lines) >= limit:
                        continue  # keep counting total lines
                    if total_chars + len(line) > MAX_OUTPUT:
                        lines.append("[truncated by size]")
                        break
                    lines.append(line.rstrip("\n"))
                    total_chars += len(line)
            end_line = offset + len(lines)
            remaining = total_lines - end_line
            header = f"[lines {offset + 1}-{end_line} of {total_lines}]"
            if remaining > 0:
                header += (
                    f" ⚠ {remaining:,} lines remaining. "
                    f'Continue with: file_read(path="{path}", offset={end_line}, limit=200)'
                )
            # Add line numbers
            numbered = [f"{offset + idx + 1:6d}\t{l}" for idx, l in enumerate(lines)]
            return header + "\n" + "\n".join(numbered)

        # Default mode: stat first so we don't load a 100MB file into RAM
        # just to hand the agent back a 50KB preview. When the file is
        # larger than the preview cap, stream the head line-by-line and
        # point the agent at this same path with offset/limit to drill in.
        size = resolved.stat().st_size
        if size > MAX_OUTPUT:
            lines: list[str] = []
            total_chars = 0
            with _open_nofollow(resolved, "r") as f:
                for line in f:
                    if total_chars + len(line) > MAX_OUTPUT:
                        break
                    lines.append(line.rstrip("\n"))
                    total_chars += len(line)
            shown = len(lines)
            header = (
                f"⚠ Large file ({size:,} bytes) — showing first {shown} lines "
                f"({total_chars:,} of ~{size:,} bytes). "
                f'Continue with: file_read(path="{path}", offset={shown}, limit=200)'
            )
            numbered = [f"{idx + 1:6d}\t{l}" for idx, l in enumerate(lines)]
            return header + "\n" + "\n".join(numbered)

        return _read_text_nofollow(resolved)
    except ValueError as e:
        return f"Error: {e}{root_mismatch_hint(path)}"
    except Exception as e:
        return f"Error reading file: {e}"


def _revision_mismatch(resolved: Path, expected: str) -> str | None:
    """Check a caller-supplied precondition against what is on disk.

    A whole-file overwrite used as read-modify-write cannot detect a stale
    read by locking its own final write — by then the caller's copy is
    already old. `expected_sha256` is the caller saying which revision it
    believes it is replacing; "absent" says it believes there is no file yet.
    The rejection names the revision that is actually there so the caller can
    re-read and decide rather than guess.
    """
    actual = file_revision(resolved)
    if expected.strip().lower() == "absent":
        if actual is None:
            return None
        return (
            f"Error: {resolved} already exists (revision {actual}) but expected_sha256='absent' — "
            f"nothing was written. Read it before overwriting."
        )
    if actual is None:
        return (
            f"Error: {resolved} does not exist, so it cannot match expected_sha256={expected} — "
            f"nothing was written. Pass expected_sha256='absent' to create it."
        )
    if actual != expected.strip().lower():
        return (
            f"Error: {resolved} is at revision {actual}, not the expected {expected} — "
            f"it changed since you read it, so nothing was written. "
            f"Call file_read(path='{resolved}') and redo the change against the current content."
        )
    return None


def file_write(path: str, content: str, expected_sha256: str | None = None) -> str:
    """Write a file to the workspace.

    Preserves the mode of a file it overwrites and creates new files at
    `atomic.NEW_FILE_MODE`. Takes the same canonical-target lock file_edit
    uses, so a write and an edit of one file serialize inside this process;
    nothing coordinates a shell redirect or another process, which is what
    `expected_sha256` is for.
    """
    cap = int(getattr(settings, "max_file_write_size", MAX_WRITE_SIZE) or MAX_WRITE_SIZE)
    if len(content) > cap:
        return f"Error: content exceeds size cap ({len(content)} > {cap} bytes)"
    try:
        resolved = _safe_write_path(path)
        with target_lock(resolved):
            if expected_sha256:
                mismatch = _revision_mismatch(resolved, expected_sha256)
                if mismatch:
                    return mismatch
            outcome = atomic_write(resolved, content)
        logger.info("file_write path=%s bytes=%d mode=%o", resolved, len(content), outcome.mode)
        note = ""
        if outcome.dropped_setuid:
            note = (
                "\n[note: the setuid bit was dropped — this tool does not re-grant setuid "
                "to content it just wrote. Re-apply with chmod if you meant it.]"
            )
        return f"Written {len(content)} chars to {resolved}{note}"
    except TargetBusy as e:
        return f"Error: {e}"
    except ValueError as e:
        return f"Error: {e}{root_mismatch_hint(path)}"
    except Exception as e:
        return f"Error writing file: {e}"


def _detect_duplicate_workspace_prefix(command: str, workspace: Path) -> str | None:
    """If the command references `data/workspace/` or `./data/workspace/` as a
    path prefix while cwd is already `data/workspace`, return a warning hint
    that will be prepended to the result. Pure advisory — never rewrites the
    command, since shell parsing is not our domain.
    """
    import re as _re

    ws_name = workspace.name  # "workspace"
    # Match `data/workspace/` or `./data/workspace/` appearing as a path
    # prefix (after whitespace or at start-of-string). Avoids matching inside
    # URLs, env vars, or strings that happen to contain the substring.
    pattern = _re.compile(rf"(?:^|[\s=(])\.?/?data/{_re.escape(ws_name)}/")
    if pattern.search(command):
        return (
            f"[hint: cwd is already {workspace} — paths in this command "
            f"start with 'data/{ws_name}/' and will resolve to "
            f"data/{ws_name}/data/{ws_name}/... Use paths relative to cwd.]"
        )
    return None


# Hard ceiling for bash's per-call `timeout` override. Mirrored into the tool
# registration as max_timeout so the executor's dispatch wait_for agrees with
# the clamp applied below — otherwise the outer wait fires first and the
# override is silently inert.
BASH_MAX_TIMEOUT = 30 * 60  # 30 minutes

# Upper bound on how much captured output is read back INTO MEMORY from the
# temp files. truncate_output then trims to MAX_OUTPUT; this cap only guards
# against pathological multi-GB captures being pulled into memory first.
_CAPTURE_READ_CAP = 5 * 1024 * 1024

# Upper bound on how much of a capture is streamed to a durable artifact. Same
# size, different job: this one bounds DISK, and it is what the model can still
# read after the preview has been collapsed and clipped. Both caps stay — the
# audit's complaint (3.2.1 H15) was never that they exist, it was that a
# capture cut by them left no evidence and no statement that it had been cut.
_CAPTURE_ARTIFACT_CAP = 5 * 1024 * 1024

# Copy granularity for the temp-file → artifact stream. 1 MiB keeps peak
# memory flat regardless of how much a command printed.
_CAPTURE_COPY_CHUNK = 1024 * 1024


def _capture_evidence(f, stream: str, *, persist: bool = True) -> tuple[str, dict]:
    """Read one capture temp file back as (preview_text, acquisition_meta).

    Evidence first, rendering second. The temp file is the only place the
    command's full output ever existed, and it is unlinked the moment bash's
    `with` block closes — so anything past the preview cap is copied to a
    durable artifact BEFORE the caller collapses repeated lines and clips to
    50 KB. The audit measured the old order costing 6,157,089 chars of a
    build log, including the unique end marker that carried the diagnosis.

    Returns the acquisition record even when nothing was lost; the caller
    decides what to say about it (a complete capture says nothing).
    """
    from core.tools.truncation import acquisition_meta, new_artifact_path, write_artifact_meta

    try:
        size = f.seek(0, os.SEEK_END)
    except (OSError, ValueError):
        return "", acquisition_meta(source=f"bash {stream}", captured=0, source_total=0)
    if not size:
        return "", acquisition_meta(source=f"bash {stream}", captured=0, source_total=0)

    artifact = ""
    captured = size
    # Small captures are wholly present in the preview; a second copy on disk
    # would be pure noise (and a file the cleanup sweep has to carry).
    if persist and size > MAX_OUTPUT:
        captured = min(size, _CAPTURE_ARTIFACT_CAP)
        try:
            path = new_artifact_path(f"bash_{stream}")
            f.seek(0)
            remaining = captured
            with open(path, "wb") as dest:
                while remaining > 0:
                    block = f.read(min(_CAPTURE_COPY_CHUNK, remaining))
                    if not block:
                        break
                    dest.write(block)
                    remaining -= len(block)
            artifact = str(path)
        except (OSError, ValueError) as e:
            logger.warning("Could not persist %s capture evidence: %s", stream, e)
            captured = min(size, _CAPTURE_READ_CAP)

    meta = acquisition_meta(
        source=f"bash {stream}",
        captured=captured,
        source_total=size,
        unit="bytes",
        truncation_reason=f"the {_CAPTURE_ARTIFACT_CAP // (1024 * 1024)} MiB process-output cap",
        artifact=artifact,
    )
    if artifact:
        write_artifact_meta(artifact, meta)

    try:
        f.seek(0)
        text = f.read(_CAPTURE_READ_CAP).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        text = ""
    return text, meta


def _collapse_for_preview(raw: str, metas: list[dict]) -> tuple[str, list[dict]]:
    """Run the readability collapse, keeping a durable copy of what it ate.

    Collapse is a presentation transform and a lossy one: 2000 identical
    WARNING lines reach the model as 4. Above the preview cap each stream
    already has its own raw artifact, so nothing more is needed. Below it —
    40 KB of library banners, say — the collapsed rendering would otherwise be
    the only surviving copy, and the audit found exactly that: an artifact
    holding 4 of 2000 lines.
    """
    from core.tools.truncation import acquisition_meta, write_artifact

    collapsed = _collapse_repeated_lines(raw)
    if collapsed == raw or any(m.get("artifact") for m in metas):
        return collapsed, metas
    meta = acquisition_meta(
        source="bash output (pre-collapse)",
        captured=len(raw.encode("utf-8", "ignore")),
        source_total=len(raw.encode("utf-8", "ignore")),
        unit="bytes",
    )
    path = write_artifact(raw, "bash_stdout", meta=meta)
    if not path:
        return collapsed, metas
    meta["artifact"] = path
    return collapsed, [*metas, meta]


def _prepend_acquisition_notes(output: str, metas: list[dict]) -> str:
    """Put the completeness statement where a head-truncation cannot cut it.

    Leading, not trailing: everything downstream of bash clips from the end,
    so a footer describing what was lost is the first thing lost.
    """
    from core.tools.truncation import acquisition_notes

    notes = acquisition_notes([m for m in metas if m])
    if not notes:
        return output
    return f"{notes}\n{output}" if output else notes


def _read_capture(f) -> str:
    """Preview text for one capture, with no artifact written.

    core/gates.py runs its own capture pairs through this and reports a short
    excerpt; a gate does not need a durable evidence copy of every check's
    output, and writing one would put a file in the tool-output dir for every
    gate run in every turn.
    """
    return _capture_evidence(f, "output", persist=False)[0]


def bash(command: str, timeout: int | None = None, _context: dict | None = None) -> str | tuple[str, dict]:
    """Execute a shell command in the workspace.

    Returns (output, metadata) once a process was launched — exit_code,
    timed_out, cwd and truncation — so the executor reads the outcome from
    the process rather than guessing at the text. A refusal that never
    launched anything (empty command, shell policy) returns the bare "Error:"
    string it always did.

    timeout: optional per-call override (seconds) for the shell timeout. Use
    when a single long-running command (Whisper transcription, large clone,
    expensive build) needs more than the default settings.shell_timeout.
    Capped at 30 minutes to prevent runaway agents from holding the worker
    indefinitely. Defaults to settings.shell_timeout when omitted.
    """
    blocked = check_shell_command(command)
    if blocked:
        return blocked

    workspace = _workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    # Space sessions run in their home folder; everyone else this is just
    # the workspace root. Venv/PATH stay on the global workspace either way
    # — the toolchain is shared, only the working directory moves.
    run_dir = _workspace_home()
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        cwd_display = str(run_dir.relative_to(Path.cwd()))
    except ValueError:
        cwd_display = str(run_dir)

    # Non-invasive advisory: flag duplicate-workspace-prefix mistakes in the
    # output so the agent notices without us rewriting arbitrary shell.
    _path_hint = _detect_duplicate_workspace_prefix(command, workspace)

    # Venv + env-mode filter + sandbox PATH/HOME/VIRTUAL_ENV, shared with
    # job_start so a background job sees the same toolchain bash does.
    env = _build_shell_env(workspace, run_dir)

    try:
        import resource
        import signal

        as_limit = int(getattr(settings, "shell_address_space_limit_bytes", 0) or 0)
        fsize_limit = int(getattr(settings, "shell_fsize_limit_bytes", 0) or 0)

        def _child_setup():
            """Applied in child process: new session + resource limits."""
            os.setsid()  # New process group so we can kill the whole tree
            try:
                if as_limit > 0:
                    resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
                if fsize_limit > 0:
                    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_limit, fsize_limit))
            except (ValueError, resource.error):
                pass

        # Capture to unlinked temp files, not pipes. With PIPE + communicate()
        # the tool returns only on pipe EOF — so a backgrounded compound list
        # (`cd app && nohup server > log 2>&1 &`) left bash's wrapper subshell
        # holding the pipe fds for the server's lifetime, and the call "hung"
        # until shell_timeout even though every foreground command finished in
        # seconds (session b23ffafde5ba: two exact-600s stalls). wait() returns
        # when the shell exits, regardless of what grandchildren still hold
        # the capture fds.
        with (
            tempfile.TemporaryFile(mode="w+b") as out_f,
            tempfile.TemporaryFile(mode="w+b") as err_f,
        ):
            process = subprocess.Popen(
                command,
                shell=True,
                executable="/bin/bash",  # bash-only features (source, <<<, $'...') work
                stdout=out_f,
                stderr=err_f,
                cwd=str(run_dir),
                env=env,
                preexec_fn=_child_setup,
            )

            # Track process on session so cancel and dispatch-timeout can kill it.
            # Registered under this dispatch's call id: two concurrent bash calls in
            # one session must not overwrite each other's entry, or the loser
            # becomes unkillable and holds its executor thread until the child
            # exits on its own.
            _session = _get_session_from_context(_context)
            _proc_handle = None
            if _session:
                _proc_handle = _session.register_process(process, (_context or {}).get("_call_id", ""))

            # Resolve effective timeout: caller override (capped at 30 min)
            # falls back to global setting. Negative/zero treated as "use default".
            if timeout is not None and int(timeout) > 0:
                effective_timeout = min(int(timeout), BASH_MAX_TIMEOUT)
            else:
                effective_timeout = settings.shell_timeout

            try:
                process.wait(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                _kill_process_tree(process)
                # Include what the command managed to print — the difference
                # between "hung silently" and "hung after X" is usually the
                # whole diagnosis.
                _out_text, _out_meta = _capture_evidence(out_f, "stdout")
                _err_text, _err_meta = _capture_evidence(err_f, "stderr")
                partial = (_out_text + _err_text).strip()
                msg = f"Error: Command timed out after {effective_timeout}s"
                if partial:
                    msg += f"\n[partial output before timeout]\n{partial[-2000:]}"
                # 2000 chars is a glance, not the record. A build that ran for
                # 30 minutes and then timed out printed everything it knew
                # before it hung, and that evidence outlives the temp files.
                msg = _prepend_acquisition_notes(msg, [_out_meta, _err_meta])
                # Pointer at the moment of pain (ARC-3 retest field case: two
                # solver timeouts, 600s and 1800s, with job_start never
                # considered — scout-time steering alone doesn't reach the
                # moment of need).
                if settings.jobs_enabled:
                    msg += (
                        "\n[harness hint] For compute that needs longer than this "
                        "timeout, job_start runs it detached with no wall limit on "
                        "your turn — poll job_status/job_tail while you keep working."
                    )
                # An infrastructure failure, not a command that failed: the
                # tool never got a verdict out of the process. It keeps the
                # "Error:" prefix and counts against bash's own health.
                return msg, {
                    "exit_code": None,
                    "timed_out": True,
                    "cwd": cwd_display,
                    "truncated": False,
                    "total_chars": len(partial),
                    "was_error": True,
                }
            finally:
                if _session and _proc_handle is not None:
                    _session.release_process(_proc_handle)

            # Evidence to disk before the preview transforms run over it.
            stdout, out_meta = _capture_evidence(out_f, "stdout")
            stderr, err_meta = _capture_evidence(err_f, "stderr")
            acquired = [m for m in (out_meta, err_meta) if m.get("captured")]

        output = ""
        if stdout:
            output += stdout
        if stderr:
            if output:
                output += "\n"
            output += stderr

        output, acquired = _collapse_for_preview(output, acquired)

        trunc = {"truncated": False, "total_chars": len(output)}
        if len(output) > MAX_OUTPUT:
            output, trunc = truncate_output(output, "bash", sources=acquired)
        else:
            # Collapse can shrink 11 MB of repeated warnings to a few hundred
            # chars, so a short result is no evidence that a short source
            # produced it. State any loss even when nothing was truncated.
            output = _prepend_acquisition_notes(output, acquired)

        rc = process.returncode

        # Prepend CWD context so the agent always knows what directory bash
        # runs in, and the exit status so it never has to infer the outcome
        # from the prose. The status used to appear only when the command
        # printed nothing at all — so "3 failed, 0 passed" and exit 1 read
        # exactly like a pass, to the model and to the harness alike.
        prefix = f"[cwd: {cwd_display}] [exit: {rc}]\n"
        if _path_hint:
            prefix = f"{_path_hint}\n{prefix}"

        # The structured channel the executor already supports for (str, dict)
        # returns. `was_error` is the tool's own verdict on the call and
        # overrides the executor's string-prefix guess, which the cwd header
        # had defeated for every command bash ever ran.
        meta = {
            "exit_code": rc,
            "timed_out": False,
            "cwd": cwd_display,
            "truncated": bool(trunc.get("truncated")),
            "total_chars": int(trunc.get("total_chars") or len(output)),
            "was_error": rc != 0,
        }
        if rc != 0:
            # The command failed; bash did not. Kept apart so tool health and
            # the stuck detector are not told the shell is broken every time a
            # test fails or a grep finds nothing.
            from core.tools.executor import COMMAND_FAILED_MARKER

            meta[COMMAND_FAILED_MARKER] = True

        return prefix + (output or "(no output)"), meta
    except subprocess.TimeoutExpired:
        return "Error: Command timed out"
    except Exception as e:
        return f"Error: {e}"


def _get_session_from_context(ctx: dict | None):
    """Look up AgentSession from tool context."""
    if not ctx:
        return None
    sid = ctx.get("session_id")
    if not sid:
        return None
    try:
        from sessions.manager import get_manager

        return get_manager().get(sid)
    except Exception:
        return None


def _kill_process_tree(process):
    """Kill a process and its entire process group (SIGTERM then SIGKILL).

    Every caller spawns the child with setsid() in preexec_fn, so the child's
    pid IS its pgid — use it directly. Resolving via os.getpgid() looks safer
    but is not: on macOS it raises ProcessLookupError once the shell is a
    zombie, silently skipping the group kill in exactly the case the group
    kill exists for (shell exited, backgrounded grandchildren still alive).
    """
    import signal

    pgid = process.pid

    # Graceful: SIGTERM to process group
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return  # Group already empty
    try:
        process.wait(timeout=3)
        # Shell exited — but its backgrounded children may not have. Only
        # skip the SIGKILL escalation once the whole group is gone.
        try:
            os.killpg(pgid, 0)
        except (OSError, ProcessLookupError):
            return  # Clean exit, group empty
    except subprocess.TimeoutExpired:
        pass

    # Forceful: SIGKILL to process group
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        logger.warning("Failed to kill process %d after SIGKILL", process.pid)


def register(reg) -> None:
    """Register core tools."""
    reg.register(
        name="file_read",
        func=file_read,
        description="Read a file or list a directory. Supports line-based pagination with offset/limit. Returns error for binary files.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within workspace"},
                "offset": {"type": "integer", "description": "Starting line number (0-based). Default: 0"},
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return. Default: 0 (all lines, subject to 50KB cap)",
                },
            },
            "required": ["path"],
        },
        category="core",
        tags=["read", "file", "open", "view", "inspect", "content", "list", "directory", "ls"],
        timeout=30,
        parallel_safe=True,
    )

    reg.register(
        name="file_write",
        func=file_write,
        description=(
            "Write content to a file in the workspace. Creates parent directories if needed. "
            "Keeps the mode of a file it overwrites. When you are rewriting a file you read "
            "earlier, pass expected_sha256 so a change someone else made in between is refused "
            "instead of silently overwritten."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within workspace"},
                "content": {"type": "string", "description": "File content to write"},
                "expected_sha256": {
                    "type": "string",
                    "description": (
                        "Optional read-modify-write precondition: the sha256 of the file's "
                        "current bytes (get it with bash `sha256sum <path>`), or 'absent' to "
                        "require that the file does not exist yet. The write is refused if it "
                        "does not match, and the error names the revision that is actually there."
                    ),
                },
            },
            "required": ["path", "content"],
        },
        category="core",
        tags=["write", "file", "create", "save", "output"],
        timeout=30,
        parallel_safe=False,
        safety_level="safe",
    )

    reg.register(
        name="bash",
        func=bash,
        description=(
            "Execute a shell command. Runs in /bin/bash with cwd=data/workspace "
            "(the agent workspace root, not the repo root). Write paths relative "
            "to this cwd — do NOT prepend `data/workspace/` yourself or you will "
            "hit `data/workspace/data/workspace/...`. Per-process address-space "
            "cap is configurable via settings.shell_address_space_limit_bytes "
            "(default 8 GB — high enough for Playwright/V8/NumPy). Output capped "
            "at 50KB. Covers git, curl, pip, node, python, etc. "
            "Pass `timeout` (seconds, max 1800) for commands that legitimately "
            "need more than the default — Whisper transcription, large clones, "
            "long builds. Without an override, the default shell_timeout applies. "
            "To start a long-lived background process (server, daemon), fully "
            "detach it so it survives cancellation and group cleanup: "
            "`(setsid cmd </dev/null >app.log 2>&1 &)` — then verify it with a "
            "separate short command (curl/pgrep) in a follow-up call. "
            "For heavy COMPUTE that needs minutes (solver searches, builds), "
            "prefer job_start instead — it runs detached with captured output "
            "and progress polling via job_status/job_tail."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Optional override for the per-call timeout in seconds. "
                        "Use for commands that legitimately need >180s (e.g. "
                        "Whisper transcription). Capped at 1800s (30 min)."
                    ),
                },
            },
            "required": ["command"],
        },
        category="core",
        tags=["shell", "execute", "run", "command", "terminal", "bash", "git", "curl", "pip", "python", "node", "npm"],
        timeout=settings.shell_timeout,
        # bash's schema exposes a per-call `timeout` override; without a
        # matching max_timeout the executor's wait_for would cap every call at
        # shell_timeout and the override would be inert.
        max_timeout=BASH_MAX_TIMEOUT,
        parallel_safe=False,
        safety_level="caution",
        # Identical command text is not identical state: a job finished, a
        # file changed, a server came up. The cross-round dedup cache must
        # never answer a shell call from a previous round's output.
        idempotent=False,
    )
