"""Pernix — the one place a worker's report lives, and the record that says so.

A worker's deliverable used to be addressed by convention: everybody rebuilt
`settings.workspace_dir / f".worker_{id[:12]}_summary.md"` from scratch and
hoped the worker had written it there. Workers inherit their parent's space,
so a space worker's relative `file_write` lands in the space home instead, and
every reader missed it — finalization then fabricated a global stamp from the
worker's chatty last assistant message while the real report sat one directory
away.

The fix is a record rather than a convention. Each RUN of a worker (spawn,
revival, a `message_worker` re-prompt) opens a row in `sessions.worker_run`
naming exactly one report path, derived from the space the worker's own tools
write in. The charter states that path, finalization stamps it, retrieval
reads it, and a new run retires the previous artifact by versioning it — a
valid report is never deleted to make room.

The record also carries the run's message-id boundary, which is what lets
verification be scoped to the output it actually graded (see `_latest_reflect`
in this package). Runs, artifacts and verification share one identifier on
purpose: three independent ones would disagree.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from db import models as db

logger = logging.getLogger("pernix.ext.orchestration.report")

# The shared pre-convention artifact. Adopted only under `legacy_claim`.
LEGACY_NAME = "summary.md"

# Slack on both ends of an activity window. Filesystem mtimes come from the
# kernel's coarse clock and can read up to a tick BEHIND a wall-clock reading
# taken just before the write — a file written milliseconds after a session row
# can carry an earlier timestamp than it. Two seconds absorbs that without
# widening a window that is minutes long in practice.
_WINDOW_SLACK_S = 2.0


def report_name(worker_id: str) -> str:
    """The relative filename a worker is told to write. The worker id is in
    the name, which is the whole of its provenance — a file called this can
    only be about this worker."""
    return f".worker_{worker_id[:12]}_summary.md"


def _retired_name(worker_id: str, seq: int) -> str:
    return f".worker_{worker_id[:12]}_summary.run{seq}.md"


# ---------------------------------------------------------------------------
# The durable record
# ---------------------------------------------------------------------------


def load_run(worker_id: str) -> dict | None:
    """The worker's current run record, or None for a worker that predates it."""
    try:
        row = db.get_session(worker_id)
    except Exception as e:
        logger.warning("load_run: session lookup failed for %s: %s", worker_id, e)
        return None
    raw = (row or {}).get("worker_run")
    if not raw:
        return None
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else None
    except (ValueError, TypeError) as e:
        logger.warning("load_run: malformed worker_run for %s: %s", worker_id, e)
        return None


def save_run(worker_id: str, run: dict) -> None:
    try:
        db.update_session(worker_id, worker_run=json.dumps(run))
    except Exception as e:
        logger.error("save_run failed for %s: %s", worker_id, e)


def begin_run(worker_id: str, *, workspace_home: str | None, reason: str = "") -> dict:
    """Open a new run and return its record.

    `workspace_home` is the worker's own soft default root — the space home
    when it has one, else None for the shared workspace. That is the root its
    relative `file_write` resolves against, so it is the root the report
    pointer has to name.

    A previous run's artifact is RETIRED, not removed: renamed to
    `.worker_<id>_summary.run<N>.md` and recorded, so a superseded-but-valid
    report stays recoverable while the active pointer moves on.
    """
    prior = load_run(worker_id) or {}
    seq = int(prior.get("seq") or 0) + 1
    root = Path(workspace_home or settings.workspace_dir)
    name = report_name(worker_id)

    retired = list(prior.get("retired") or [])
    prior_seq = int(prior.get("seq") or 0)
    # Retire whatever the PREVIOUS run would have served — the recorded path
    # when there is one, and otherwise whatever discovery finds, so a worker
    # that predates the record does not carry its stale stamp into a new run.
    # A shared summary.md is never touched: it is not this worker's to move.
    stale = prior.get("report_path")
    if not stale:
        found = resolve_report(worker_id)
        if found is not None and found.origin != "legacy":
            stale = str(found.path)
    if stale:
        try:
            old = Path(stale)
            if old.exists():
                dest = old.with_name(_retired_name(worker_id, max(prior_seq, 1)))
                old.replace(dest)
                retired.append({"seq": max(prior_seq, 1), "path": str(dest)})
                logger.info("Worker %s: retired run %d report to %s", worker_id[:12], max(prior_seq, 1), dest)
        except OSError as e:
            logger.warning("begin_run: could not retire %s: %s", stale, e)

    run = {
        "seq": seq,
        "reason": reason,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "started_msg_id": _last_message_id(worker_id),
        "report_path": str(root / name),
        "report_name": name,
        "retired": retired[-5:],
    }
    save_run(worker_id, run)
    logger.info(
        "Worker %s run %d opened (%s) — report %s",
        worker_id[:12],
        seq,
        reason or "unspecified",
        run["report_path"],
    )
    return run


def _last_message_id(worker_id: str) -> int:
    """The transcript boundary a new run starts above. Rows at or below it
    belong to earlier runs — including their reflect verdicts."""
    try:
        rows = db.get_messages(worker_id, last=1)
        return int(rows[-1].get("id") or 0) if rows else 0
    except Exception as e:
        logger.debug("run boundary lookup failed for %s: %s", worker_id, e)
        return 0


def run_boundary(worker_id: str) -> int | None:
    """Message id below which rows belong to a previous run, or None when the
    worker has no run record (nothing is known to be stale, so nothing is)."""
    run = load_run(worker_id)
    if run is None:
        return None
    try:
        return int(run.get("started_msg_id") or 0)
    except (TypeError, ValueError):
        return 0


def digest(path: Path) -> str:
    """sha256 of an artifact, for binding a grade to the bytes it graded."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def record_grade(worker_id: str, *, verdict: str | None, verification: str, artifact: Path | None) -> None:
    """Bind this run's grade to the bytes it graded.

    Called once a run's post-hooks have settled. Two things it makes possible:
    a later read can tell that the artifact has changed since it was graded,
    and a stamp this harness wrote can be recognized as ours without trusting
    the file's own first line — which is a line a worker can type.
    """
    run = load_run(worker_id)
    if run is None:
        return
    run["graded"] = {
        "seq": int(run.get("seq") or 1),
        "verdict": verdict,
        "verification": verification,
        "digest": digest(artifact) if artifact is not None else "",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    save_run(worker_id, run)


def record_stamp(worker_id: str, *, artifact: Path, header: str) -> None:
    """Remember that WE wrote this artifact's header, and over which bytes."""
    run = load_run(worker_id)
    if run is None:
        return
    run["stamp"] = {
        "seq": int(run.get("seq") or 1),
        "digest": digest(artifact),
        "header": header.splitlines()[0] if header else "",
    }
    save_run(worker_id, run)


def is_our_stamp(worker_id: str, ref: "ReportRef | None") -> bool:
    """True when the artifact is byte-for-byte the stamp this harness wrote
    for the CURRENT run. Everything else — including a worker-authored
    `# AUTO-STAMPED (reflect=pass...)` — is content, not a trust statement."""
    if ref is None:
        return False
    run = load_run(worker_id)
    stamp = (run or {}).get("stamp") or {}
    if not stamp or int(stamp.get("seq") or 0) != int((run or {}).get("seq") or 0):
        return False
    return bool(stamp.get("digest")) and stamp["digest"] == digest(ref.path)


def graded_artifact_changed(worker_id: str, ref: "ReportRef | None") -> bool:
    """True when the artifact has been edited since its grade was recorded.

    A verdict is a statement about bytes. If the bytes moved, the verdict did
    not move with them, and the parent is entitled to know before it acts.
    """
    if ref is None:
        return False
    run = load_run(worker_id)
    graded = (run or {}).get("graded") or {}
    if int(graded.get("seq") or 0) != int((run or {}).get("seq") or 0):
        return False
    recorded = graded.get("digest") or ""
    return bool(recorded) and recorded != digest(ref.path)


def retired_reports(worker_id: str) -> list[dict]:
    """Earlier runs' artifacts, newest last. Kept, never deleted — but they
    are that run's output, not this one's, and are labelled by run number."""
    run = load_run(worker_id)
    out = []
    for entry in (run or {}).get("retired") or []:
        try:
            if Path(entry["path"]).exists():
                out.append(entry)
        except (KeyError, OSError):
            continue
    return out


def run_scoped_reflect(worker_id: str) -> tuple[dict | None, dict | None, int]:
    """(this run's grade, an earlier run's grade, that grade's run number).

    A verdict is a statement about the output the grader read. A resumed
    worker's second run is different output, so run A's pass must not certify
    it. With no run record nothing is KNOWN to be stale, so the newest grade is
    returned as current — old workers keep the behaviour they were graded under.
    """
    try:
        messages = db.get_messages(worker_id)
    except Exception as e:
        logger.warning("run_scoped_reflect: db.get_messages(%s) failed: %s", worker_id, e)
        return None, None, 0
    newest = None
    newest_id = 0
    for m in reversed(messages):
        if m.get("role") == "reflect":
            try:
                newest = json.loads(m.get("content") or "{}")
            except (ValueError, TypeError) as e:
                logger.warning("run_scoped_reflect: malformed reflect row for %s: %s", worker_id, e)
                return None, None, 0
            newest_id = int(m.get("id") or 0)
            break
    if newest is None:
        return None, None, 0
    boundary = run_boundary(worker_id)
    if boundary is None or newest_id > boundary:
        return newest, None, 0
    run = load_run(worker_id) or {}
    return None, newest, max(int(run.get("seq") or 1) - 1, 1)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass
class ReportRef:
    """Where a worker's report actually is, and how sure we are it is its."""

    path: Path
    origin: str  # "run" | "discovered" | "legacy"
    seq: int = 0
    provenance: str = ""  # non-empty only when the link is inferred, not recorded
    label: str = ""  # a header the caller must not drop
    candidates: list = field(default_factory=list)

    def read(self) -> str:
        return self.path.read_text()


def _space_home(worker_id: str) -> Path | None:
    try:
        row = db.get_session(worker_id) or {}
        space_id = row.get("space_id")
        if not space_id:
            return None
        from core import spaces as _spaces

        space = _spaces.get_space(space_id)
        return _spaces.space_workspace_home(space) if space else None
    except Exception as e:
        logger.debug("space home lookup failed for %s: %s", worker_id, e)
        return None


def search_roots(worker_id: str) -> list[Path]:
    """Where a report for this worker could legitimately be: its space home
    first (where its own relative writes land), then the shared workspace."""
    roots: list[Path] = []
    home = _space_home(worker_id)
    if home is not None:
        roots.append(home)
    ws = Path(settings.workspace_dir)
    if ws not in roots:
        roots.append(ws)
    return roots


def resolve_report(worker_id: str) -> ReportRef | None:
    """The worker's report, by record first and discovery second.

    Discovery is narrow on purpose: only a file whose NAME carries this
    worker's id counts, so a hit carries its own proof. The shared
    `summary.md` is a third resort, gated by `legacy_claim` and always labelled.
    """
    run = load_run(worker_id)
    candidates: list[Path] = []

    if run and run.get("report_path"):
        p = Path(run["report_path"])
        candidates.append(p)
        if p.exists():
            return ReportRef(path=p, origin="run", seq=int(run.get("seq") or 1), candidates=candidates)

    name = report_name(worker_id)
    for root in search_roots(worker_id):
        p = root / name
        if p in candidates:
            continue
        candidates.append(p)
        if p.exists():
            return ReportRef(
                path=p,
                origin="discovered",
                seq=int((run or {}).get("seq") or 0),
                provenance="found by the worker id in its filename",
                candidates=candidates,
            )

    # Pre-convention compatibility. A worker WITH a run record is post-fix and
    # never owned the shared file, so it is not offered one.
    if run is None:
        legacy = Path(settings.workspace_dir) / LEGACY_NAME
        if legacy.exists():
            claim = legacy_claim(worker_id, legacy)
            if claim:
                return ReportRef(
                    path=legacy,
                    origin="legacy",
                    provenance=claim,
                    label=(
                        f"# LEGACY SUMMARY (shared {LEGACY_NAME}, written before per-worker "
                        f"reports existed)\n# Provenance is inferred, not recorded: {claim}\n"
                    ),
                    candidates=candidates + [legacy],
                )
            logger.info(
                "Worker %s: shared %s left unadopted — nothing ties it to this worker",
                worker_id[:12],
                LEGACY_NAME,
            )
    return None


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _activity_window(row: dict) -> tuple[float, float] | None:
    """When this session could plausibly have written a file: from its
    creation to its last sign of life. `sessions.updated_at` alone is not
    enough — add_message does not bump it, so a busy worker's row can still
    read as zero-width."""
    start = _epoch(row.get("created_at"))
    if start is None:
        return None
    end = _epoch(row.get("updated_at")) or start
    try:
        last = db.get_messages(row["id"], last=1)
        if last:
            end = max(end, _epoch(last[-1].get("created_at")) or end)
    except Exception as e:
        logger.debug("activity window: message lookup failed for %s: %s", row.get("id"), e)
    return (start, end) if end >= start else None


def legacy_claim(worker_id: str, legacy: Path) -> str:
    """A provenance note if the shared summary.md plausibly belongs to this
    worker, else "" — in which case it is not served at all.

    The rule is deliberately narrow. The file must have been written while
    this worker was active, and no sibling worker of the same parent may have
    been active at that moment either: one shared file cannot be two workers'
    report, and the old code served it as both.
    """
    try:
        mtime = legacy.stat().st_mtime
    except OSError:
        return ""
    row = db.get_session(worker_id) or {}
    window = _activity_window(row)
    if window is None or not (window[0] - _WINDOW_SLACK_S <= mtime <= window[1] + _WINDOW_SLACK_S):
        return ""
    parent_id = row.get("parent_session_id")
    if parent_id:
        try:
            siblings = [r for r in db.get_worker_sessions(parent_id) if r["id"] != worker_id]
        except Exception as e:
            logger.debug("legacy_claim sibling sweep failed: %s", e)
            return ""
        for sib in siblings:
            sw = _activity_window(sib)
            if sw and sw[0] - _WINDOW_SLACK_S <= mtime <= sw[1] + _WINDOW_SLACK_S:
                return ""
    when = datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds")
    return f"written {when}, inside this worker's own run window, and no sibling worker was running then"
