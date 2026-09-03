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
from core.tools.paths import (
    PROTECTED_DIRS,
    PROTECTED_FILES,
    root_mismatch_hint,
)
from core.tools.paths import (
    allowed_read_roots as _allowed_roots,
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


def _rm_targets_are_safe_caches(command: str) -> bool:
    """Return True iff every non-flag argument to `rm` is a cache directory.

    Used to permit `rm -rf __pycache__` and similar without lifting the broader
    `rm -rf` block. Conservative: any non-cache target (file path, absolute
    path, glob, env var, parent-traversal, multiple tokens with one unsafe)
    fails the check and keeps the original block in place.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False  # parse failure → don't take the safe path
    if not tokens or tokens[0] != "rm":
        return False
    targets: list[str] = []
    for tok in tokens[1:]:
        if tok.startswith("-"):
            continue
        targets.append(tok)
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
    cwd=workspace and the literal prefix already pins the tree."""
    from core.tools.paths import workspace

    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or tokens[0] != "rm":
        return False
    ws = workspace()
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


def file_write(path: str, content: str) -> str:
    """Write a file to the workspace."""
    import fcntl
    import tempfile

    cap = int(getattr(settings, "max_file_write_size", MAX_WRITE_SIZE) or MAX_WRITE_SIZE)
    if len(content) > cap:
        return f"Error: content exceeds size cap ({len(content)} > {cap} bytes)"
    try:
        resolved = _safe_write_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to temp file, then rename
        fd, tmp_path = tempfile.mkstemp(dir=str(resolved.parent), suffix=".tmp", prefix=f".{resolved.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(resolved))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info("file_write path=%s bytes=%d", resolved, len(content))
        return f"Written {len(content)} chars to {resolved}"
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

# Upper bound on how much captured output is read back from the temp files.
# truncate_output then trims to MAX_OUTPUT; this cap only guards against
# pathological multi-GB captures being pulled into memory first.
_CAPTURE_READ_CAP = 5 * 1024 * 1024


def _read_capture(f) -> str:
    """Read a binary capture temp file back from the start (bounded by _CAPTURE_READ_CAP)."""
    try:
        size = f.seek(0, os.SEEK_END)
        f.seek(0)
        data = f.read(_CAPTURE_READ_CAP).decode("utf-8", errors="replace")
        if size > _CAPTURE_READ_CAP:
            data += f"\n[... output truncated: {size - _CAPTURE_READ_CAP} more bytes not shown ...]"
        return data
    except (OSError, ValueError):
        return ""


def bash(command: str, timeout: int | None = None, _context: dict | None = None) -> str:
    """Execute a shell command in the workspace.

    timeout: optional per-call override (seconds) for the shell timeout. Use
    when a single long-running command (Whisper transcription, large clone,
    expensive build) needs more than the default settings.shell_timeout.
    Capped at 30 minutes to prevent runaway agents from holding the worker
    indefinitely. Defaults to settings.shell_timeout when omitted.
    """
    if not command or not command.strip():
        return "Error: Empty command"

    if settings.shell_security_mode == "permissive":
        blocked = _check_command_security(command)
        if blocked:
            return blocked
    elif settings.shell_security_mode == "strict":
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word not in settings.shell_allowlist:
            return f"Error: Command '{first_word}' not in allowlist. Allowed: {', '.join(sorted(settings.shell_allowlist)[:10])}..."

    workspace = _workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    # Space sessions run in their home folder; everyone else this is just
    # the workspace root. Venv/PATH stay on the global workspace either way
    # — the toolchain is shared, only the working directory moves.
    run_dir = _workspace_home()
    run_dir.mkdir(parents=True, exist_ok=True)

    # Non-invasive advisory: flag duplicate-workspace-prefix mistakes in the
    # output so the agent notices without us rewriting arbitrary shell.
    _path_hint = _detect_duplicate_workspace_prefix(command, workspace)

    # Ensure workspace venv exists for Python/pip isolation
    venv_dir = workspace / ".venv"
    if not (venv_dir / "bin" / "python").exists():
        import sys as _sys

        subprocess.run([_sys.executable, "-m", "venv", str(venv_dir)], capture_output=True, timeout=60)

    # Build environment based on configured mode
    if settings.shell_env_mode == "passthrough":
        env = dict(os.environ)
    elif settings.shell_env_mode == "denylist":
        denied = set(settings.shell_env_denylist)
        env = {k: v for k, v in os.environ.items() if k not in denied}
    else:  # allowlist
        allowed = set(settings.shell_env_allowlist)
        env = {k: v for k, v in os.environ.items() if k in allowed}
    # Always override PATH and HOME for sandbox
    # Prepend workspace venv bin so pip/python resolve to venv, not system
    workspace_venv_bin = str(workspace / ".venv" / "bin")
    env["PATH"] = f"{workspace_venv_bin}:/usr/local/bin:/usr/bin:/bin"
    env["HOME"] = str(run_dir)
    env["VIRTUAL_ENV"] = str(workspace / ".venv")
    # Python block-buffers stdout when it isn't a tty, so a long-running
    # script's progress prints sit in an unflushed buffer — and the
    # [partial output before timeout] block comes back empty exactly when
    # it matters most (field case c93232a0521b: a 30-minute search printed
    # progress the whole way and the timeout returned none of it).
    env["PYTHONUNBUFFERED"] = "1"

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
                partial = (_read_capture(out_f) + _read_capture(err_f)).strip()
                msg = f"Error: Command timed out after {effective_timeout}s"
                if partial:
                    msg += f"\n[partial output before timeout]\n{partial[-2000:]}"
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
                return msg
            finally:
                if _session and _proc_handle is not None:
                    _session.release_process(_proc_handle)

            stdout = _read_capture(out_f)
            stderr = _read_capture(err_f)

        output = ""
        if stdout:
            output += stdout
        if stderr:
            if output:
                output += "\n"
            output += stderr

        output = _collapse_repeated_lines(output)

        if len(output) > MAX_OUTPUT:
            output, _meta = truncate_output(output, "bash")

        if process.returncode != 0 and not output:
            output = f"Exit code: {process.returncode}"

        # Prepend CWD context so the agent always knows what directory bash runs in
        try:
            cwd_display = run_dir.relative_to(Path.cwd())
        except ValueError:
            cwd_display = run_dir
        prefix = f"[cwd: {cwd_display}]\n"
        if _path_hint:
            prefix = f"{_path_hint}\n{prefix}"

        return prefix + (output or "(no output)")
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
        description="Write content to a file in the workspace. Creates parent directories if needed.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within workspace"},
                "content": {"type": "string", "description": "File content to write"},
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
    )
