"""Pernix — the one atomic write the file tools share, and the lock that
makes a read-modify-write interval mean something.

Two writers lived here before this module did: `file_edit._atomic_write` and
the inline block in `core_tools.file_write`. They had drifted into the same
two defects, and fixing one would have left the other.

Mode. Both built the replacement with `tempfile.mkstemp`, which creates its
file at 0600 whatever the umask says, and then `os.replace`d it over the
target without ever looking at the mode the target had. The result was
exactly 0600 every time — a 0755 script stopped being executable, a 0644
asset stopped being readable by anyone else, a 0640 config lost its group
read, setgid vanished. `atomic_write` stats the target and fchmods the temp
file to that mode before the replace.

Locking. Both took an `fcntl.flock` on the freshly-mkstemp'd temp fd — a file
no other process has ever been able to name — so neither could block
anything. The real interval to protect is read → transform → replace, and it
lives in the caller, so the lock lives here as `target_lock` and the caller
holds it across the whole thing.

`target_lock` is process-local. It serializes every editor inside this
process — parent and worker, two tool rounds, two sessions — which is what
`parallel_safe=False` never did, since the executor only uses that flag to
keep calls off one session's gather batch. It does not coordinate a shell
`sed -i`, a second Pernix process, or a human with an editor open. Callers
that care re-read the target immediately before replacing it and refuse when
the bytes moved; a write landing between that check and `os.replace` is
still lost. That race is disclosed, not closed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, NamedTuple

logger = logging.getLogger("pernix.tools.atomic")

# The mode a file created by these tools gets. mkstemp already landed here,
# but it landed here by accident; naming it makes the default a decision.
# 0600 keeps agent-authored content out of other uids' reach by default —
# widening it is `chmod`, one deliberate command away.
NEW_FILE_MODE = 0o600

# How long a caller waits for another editor to finish with the same file
# before giving up. Matches the file_edit tool timeout, so a caller that is
# going to be killed by the executor anyway gets a legible error first.
LOCK_TIMEOUT = 30.0


class TargetBusy(RuntimeError):
    """Another editor held the target for longer than the caller could wait."""


class WriteOutcome(NamedTuple):
    """What `atomic_write` did, for callers that need to say so."""

    mode: int
    created: bool
    dropped_setuid: bool


# ---------------------------------------------------------------------------
# Canonical-target locks
# ---------------------------------------------------------------------------

_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_holders: dict[str, int] = {}


@contextmanager
def target_lock(resolved: Path | str, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Hold the lock for one canonical target path for the whole `with` body.

    Keyed on the fully-resolved path, so two spellings of the same file take
    the same lock. Re-entrant per thread, and the registry entry is dropped
    when the last holder leaves so a long-lived process editing many files
    does not accumulate a lock per path it has ever touched.

    Process-local only — see the module docstring for what that does not
    cover. Raises `TargetBusy` rather than blocking forever.
    """
    key = os.fspath(resolved)
    with _registry_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        _holders[key] = _holders.get(key, 0) + 1
    try:
        if not lock.acquire(timeout=timeout):
            raise TargetBusy(
                f"{key} is being edited by another call and did not become free "
                f"within {timeout:.0f}s — nothing was written. Retry, or check "
                f"whether two workers are editing the same file."
            )
        try:
            yield
        finally:
            lock.release()
    finally:
        with _registry_guard:
            remaining = _holders.get(key, 1) - 1
            if remaining <= 0:
                _holders.pop(key, None)
                _locks.pop(key, None)
            else:
                _holders[key] = remaining


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


def content_revision(content: str | bytes) -> str:
    """The revision id for a piece of content: sha256 of its bytes."""
    data = content.encode() if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def file_revision(resolved: Path) -> str | None:
    """The revision id of what is on disk right now, or None if nothing is."""
    try:
        with open(resolved, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def _mode_for(resolved: Path) -> tuple[int, bool, bool]:
    """Return (mode to write, setuid was dropped, target did not exist).

    Stats without following symlinks: `safe_write_path` already resolved the
    path, so anything that is a symlink here was swapped in afterwards, and a
    symlink's own 0777 is not a mode to inherit. Ownership is never copied —
    the file keeps whatever uid/gid the replace gives it.
    """
    try:
        st = os.stat(resolved, follow_symlinks=False)
    except FileNotFoundError:
        return NEW_FILE_MODE, False, True
    except OSError as e:  # pragma: no cover - unreadable parent, EACCES, ...
        logger.warning("cannot stat %s (%s) — writing at the default mode", resolved, e)
        return NEW_FILE_MODE, False, False

    if not stat.S_ISREG(st.st_mode):
        logger.warning("%s is not a regular file — writing at the default mode", resolved)
        return NEW_FILE_MODE, False, False

    existing = stat.S_IMODE(st.st_mode)
    # setgid stays: its blast radius is a group the file already belonged to,
    # and shared build trees and group-writable checkouts rely on it, so
    # dropping it breaks the workflow that set it and protects no one.
    # setuid goes: it hands an arbitrary uid — usually root — to bytes this
    # process just authored, which is the sharpest escalation available, and
    # no edit needs to re-grant it. Callers say so in their result.
    return existing & ~stat.S_ISUID, bool(existing & stat.S_ISUID), False


def atomic_write(resolved: Path, content: str, newline: str = "") -> WriteOutcome:
    """Replace `resolved` with `content`, atomically, at the mode it had.

    Temp file in the same directory, content written, mode set, fsync, then
    `os.replace` — so a reader sees either the old file or the whole new one,
    never a half-written one, and never one at the wrong mode. fchmod runs
    before the fsync so the metadata change is covered by it. A failure at
    any point removes the temp file and leaves the target alone.

    `newline=""` writes the caller's line endings verbatim; `file_edit`
    depends on that to preserve CRLF and lone-CR files.

    Does NOT lock. The interval worth locking is the caller's — take
    `target_lock` around read-transform-write and call this inside it.
    """
    resolved.parent.mkdir(parents=True, exist_ok=True)
    mode, dropped_setuid, created = _mode_for(resolved)

    fd, tmp_path = tempfile.mkstemp(dir=str(resolved.parent), suffix=".tmp", prefix=f".{resolved.name}.")
    try:
        with os.fdopen(fd, "w", newline=newline) as f:
            f.write(content)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp_path, str(resolved))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return WriteOutcome(mode=mode, created=created, dropped_setuid=dropped_setuid)
