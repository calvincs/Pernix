"""TELOS object store: markdown files with YAML frontmatter under data/telos/.

Layout (spec §1):
    data/telos/
      config/telos.yaml          layer provenance record (readable tier)
      questions/q_*.md           Question objects
      soup/h_*.md                hypotheses: speculation pool + gated + resolved
      soup/archive/h_*.md        terminal hypotheses, out of the scan path
      goals/g_*.md               root, dreams, milestones, tasks
      claims/c_*.md              committed claims with epistemic class caps
      alarms/a_*.md              binding | hevel | divergence | acedia
      ledgers/first_person/      autobiography (agent-writable, weekly)
      ledgers/trace/             append-only JSONL, one file per UTC day

Markdown-as-database on purpose: BM25/ripgrep is the query layer and every
state transition is a diffable file edit. Acceptable at single-operator
scale; revisit past ~10^3 questions/week (spec §8).

The trace is authoritative over the autobiography (spec §5.4). Filesystem
mounts are out of reach here, so the authority ordering is enforced at the
API surface instead: nothing in this package exposes a trace-rewrite path,
trace_append is the only writer, and the agent-facing tools get no write
access to ledgers/trace/.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import ClassVar
from datetime import datetime, timezone
from pathlib import Path

import yaml

from config import settings

logger = logging.getLogger("pernix.telos.store")

# Humility layer (spec §6): hard confidence caps by epistemic class,
# enforced at claim commit. self_report corroborated by the trace is
# reclassified observation_of_self and escapes the cap.
EPISTEMIC_CAPS = {
    "observation": 0.99,
    "inference": 0.95,
    "testimony": 0.90,
    "analogy": 0.70,
    "self_report": 0.60,
    "observation_of_self": 0.99,
}

# 'closed' was removed in the v3.1 carve: it was declared but no code path
# ever wrote it, and two (now-deleted) detectors counted the phantom event.
QUESTION_STATES = ("open", "narrowed", "abandoned")

# Terminal hypothesis statuses. Both live in soup/archive/ and neither comes
# back: there is no un-archive path, because a hypothesis still worth asking
# re-mints cheaply from its question. They are two statuses rather than one
# on purpose — the calibration review reads them as different failure classes:
#
#   untestable — examined and unresolvable. Either the evaluator spent its
#                attempts and stayed inconclusive, or the mint-time gate made
#                a structural diagnosis (no falsifier named, or the observable
#                is absent from the records evaluation can read).
#   expired    — aged out of the pool without ever being examined. Says
#                nothing about the hypothesis; only that its turn never came.
#
# Note what is NOT terminal: an eig-below-floor gate reason. Low expected
# information gain is a prior about how much answering would teach, not a
# statement that it cannot be answered — a different axis, and the pool keeps
# those entries.
ARCHIVED_HYPOTHESIS_STATES = ("untestable", "expired")
HYPOTHESIS_STATES = ("soup", "gated", "running", "supported", "refuted") + ARCHIVED_HYPOTHESIS_STATES
# v3.1 carve: the goal DAG is gone (only g_root survives, as the question
# tree's anchor), and with it the binding/hevel/divergence alarm minters.
GOAL_KINDS = ("root",)
ALARM_TYPES = ("acedia",)

# An alarm is *live* while its signature is still on the books. Operator
# acknowledgement silences the notification, it does not retire the evidence:
# an acked alarm stays the same alarm so the escalation ladder keeps its
# place instead of minting a fresh L1 on the next monitor pass.
LIVE_ALARM_STATES = ("open", "acknowledged")
# A cleared alarm is *discharged*: the condition measurably stopped holding
# (its own monitor, or the E3 discharge pass's N spaced clean re-checks).
# Distinct from acknowledged — evidence retired, not notification silenced.
CLOSED_ALARM_STATES = ("cleared",)

_SLUG_RE = re.compile(r"[^a-z0-9_]+")

# mint_id derives its sequence from a directory listing, and both TELOS loops
# (fast-loop snooze, slow-loop cron) mint inside one process — without this
# two threads read the same listing, mint the same c_NNNN, and the second
# write silently overwrites the first. A same-process lock plus a reservation
# set is sufficient: nothing outside this process mints ids for the store.
_MINT_LOCK = threading.Lock()
_MINT_RESERVED: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _slug(text: str, max_len: int = 40) -> str:
    return _SLUG_RE.sub("_", text.lower()).strip("_")[:max_len] or "x"


@dataclass
class TelosObject:
    """One markdown object: frontmatter dict + free-text body."""

    id: str
    kind: str  # question | hypothesis | goal | claim | alarm
    meta: dict
    body: str = ""
    path: Path | None = None

    def get(self, key, default=None):
        return self.meta.get(key, default)


@dataclass
class TelosStore:
    root: Path
    _seq_cache: dict = field(default_factory=dict)

    # ledgers/first_person left with the reconciliation carve (v3.1); an
    # existing directory on disk is simply never written again.
    _DIRS = (
        "config",
        "questions",
        "soup",
        "goals",
        "claims",
        "alarms",
        "ledgers/trace",
    )
    # open() used to re-mkdir every directory on every call — and it is
    # called per compile, per turn, per API hit. Ensure once per process
    # (keyed by root path, so tests with per-test tmp dirs still ensure).
    _dirs_ensured: ClassVar[set] = set()

    _KIND_DIRS = {
        "question": "questions",
        "hypothesis": "soup",
        "goal": "goals",
        "claim": "claims",
        "alarm": "alarms",
    }

    # Terminal objects move one level down, into <kind dir>/archive/. Every
    # scan in this class globs a single directory level, so an archived file
    # is invisible to the hot path by construction rather than by remembering
    # to filter on status at each call site.
    _ARCHIVE_SUBDIR = "archive"

    @classmethod
    def open(cls) -> "TelosStore":
        root = Path(settings.telos_dir)
        store = cls(root=root)
        key = str(root)
        if key not in cls._dirs_ensured:
            for d in cls._DIRS:
                (root / d).mkdir(parents=True, exist_ok=True)
            cls._dirs_ensured.add(key)
        return store

    # ------------------------------------------------------------------
    # Object IO
    # ------------------------------------------------------------------

    def _dir_for(self, kind: str) -> Path:
        return self.root / self._KIND_DIRS[kind]

    def _archive_dir(self, kind: str, create: bool = False) -> Path:
        d = self._dir_for(kind) / self._ARCHIVE_SUBDIR
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def mint_id(self, kind: str, hint: str = "") -> str:
        """Provenas-style ids: q_2026_0807_003, h_0042, g_<slug>, c_0007, a_0003."""
        prefix = {"question": "q", "hypothesis": "h", "goal": "g", "claim": "c", "alarm": "a"}[kind]
        if kind == "goal":
            return f"g_{_slug(hint)}"
        d = self._dir_for(kind)
        if kind == "question":
            stamp = datetime.now(timezone.utc).strftime("%Y_%m%d")
            stem, width = f"q_{stamp}_", 3
        else:
            stem, width = f"{prefix}_", 4
        # Ids must be monotonic, not merely unused. Deriving the next number
        # from what is on disk means deleting a file frees its id for reuse —
        # and a reused id silently re-points every claim, trace event and
        # `derived_from` edge that still names it. Retention (archiving the
        # speculation pool out of this listing, and the archive's own
        # hard-delete horizon) is therefore only safe against a persisted
        # high-water mark, which is what this reads and advances.
        with _MINT_LOCK:
            hw_key = f"id_high_water_{prefix}" if kind != "question" else f"id_high_water_q_{stamp}"
            state = self.get_state()
            try:
                high_water = int(state.get(hw_key, 0) or 0)
            except (TypeError, ValueError):
                high_water = 0
            # max() with the disk scan keeps stores predating the high-water
            # mark correct on their first mint after upgrade.
            n = max(high_water, len(list(d.glob(f"{stem}*.md")))) + 1
            while True:
                path = d / f"{stem}{n:0{width}d}.md"
                if not path.exists() and str(path) not in _MINT_RESERVED:
                    _MINT_RESERVED.add(str(path))
                    self.set_state(**{hw_key: n})
                    return f"{stem}{n:0{width}d}"
                n += 1

    def write(self, obj: TelosObject) -> Path:
        """Atomic write of one object (mkstemp + os.replace, config.py pattern).

        Always writes to the kind's live directory, never to the archive —
        `archive_hypothesis` is the only way a file gets down there, and the
        only way one leaves is `prune_soup_archive` unlinking it.
        """
        d = self._dir_for(obj.kind)
        path = d / f"{obj.id}.md"
        meta = dict(obj.meta)
        meta["id"] = obj.id
        meta.setdefault("created_at", _now_iso())
        meta["updated_at"] = _now_iso()
        front = yaml.safe_dump(meta, sort_keys=True, allow_unicode=True, default_flow_style=False)
        content = f"---\n{front}---\n\n{obj.body.strip()}\n" if obj.body.strip() else f"---\n{front}---\n"
        fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        with _MINT_LOCK:
            _MINT_RESERVED.discard(str(path))
        obj.path = path
        return path

    def read(self, kind: str, obj_id: str) -> TelosObject | None:
        path = self._dir_for(kind) / f"{obj_id}.md"
        return self._parse(kind, path)

    def _parse(self, kind: str, path: Path) -> TelosObject | None:
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        meta: dict = {}
        body = text
        if text.startswith("---\n"):
            end = text.find("\n---", 4)
            if end != -1:
                try:
                    meta = yaml.safe_load(text[4:end]) or {}
                except yaml.YAMLError:
                    logger.warning("telos: bad frontmatter in %s", path)
                    meta = {}
                body = text[end + 4 :].lstrip("\n")
        if not isinstance(meta, dict):
            meta = {}
        return TelosObject(id=meta.get("id", path.stem), kind=kind, meta=meta, body=body.strip(), path=path)

    def list(self, obj_kind: str, **filters) -> list[TelosObject]:
        """All objects of a kind, oldest-first by filename, filtered by
        frontmatter equality (state='open', parent='g_root', kind='dream'...).
        First param is positional-only in spirit: 'kind' stays available as a
        frontmatter filter (goals use it for root|dream|milestone|task).

        The glob is one level deep on purpose: <kind>/archive/ is not scanned,
        so terminal objects cost nothing here and drop out of every count
        built on this method."""
        return self._list_dir(self._dir_for(obj_kind), obj_kind, filters)

    def _list_dir(self, d: Path, obj_kind: str, filters: dict) -> list[TelosObject]:
        out = []
        for p in sorted(d.glob("*.md")):
            obj = self._parse(obj_kind, p)
            if obj is None:
                continue
            if all(obj.get(k) == v for k, v in filters.items()):
                out.append(obj)
        return out

    def update(self, obj: TelosObject, **changes) -> TelosObject:
        obj.meta.update(changes)
        self.write(obj)
        return obj

    # ------------------------------------------------------------------
    # Archive (terminal objects, out of the scan path)
    # ------------------------------------------------------------------

    def archive_hypothesis(self, obj: TelosObject, status: str, reason: str, **changes) -> Path | None:
        """Stamp a terminal status on a hypothesis and move it to soup/archive/.

        Changing the status alone would not be enough. The cost of a dead
        entry is file count, not status: `list()` re-reads every file in
        soup/ on every generate and evaluate pass, so an entry that can never
        run again is charged to the hot path forever unless its file leaves
        the scanned directory. Moving it is what makes a terminal verdict
        free, and it is also what takes the entry out of the pool counts —
        both follow from the one-level glob, with no filter to remember.

        Nothing is deleted here. The pool doubles as the calibration review's
        forensic record, and `gate_reason` — the diagnosis that put the entry
        in the pool — is preserved; the archive's own `archive_reason` says
        why it left. Ids stay retired either way: `mint_id` counts against a
        persisted high-water mark, not the directory listing.

        Write-then-move, in that order. A crash between the two steps leaves
        one correctly-stamped file still in soup/, which the next sweep
        retries; move-then-write could leave two.
        """
        if obj.kind != "hypothesis":
            raise ValueError(f"archive_hypothesis: not a hypothesis ({obj.kind})")
        if status not in ARCHIVED_HYPOTHESIS_STATES:
            raise ValueError(f"archive_hypothesis: {status!r} is not a terminal status")
        obj.meta.update(changes)
        obj.meta["status"] = status
        obj.meta["archive_reason"] = str(reason)[:300]
        obj.meta["archived_at"] = _now_iso()
        src = self.write(obj)
        dest = self._archive_dir(obj.kind, create=True) / f"{obj.id}.md"
        try:
            os.replace(src, dest)
        except OSError as e:
            logger.warning("telos: archiving %s failed, left in place: %s", obj.id, e)
            return None
        obj.path = dest
        return dest

    def list_archived(self, obj_kind: str, **filters) -> list[TelosObject]:
        """Archived objects, same filtering as `list()`. Retention passes and
        forensic readers only — nothing on the fast loop calls this, which is
        the entire point of moving the files."""
        d = self._archive_dir(obj_kind)
        return self._list_dir(d, obj_kind, filters) if d.is_dir() else []

    def read_archived(self, kind: str, obj_id: str) -> TelosObject | None:
        """Explicit archive read. `read()` deliberately does NOT fall through
        to here: live callers treat a missing hypothesis as gone, and an
        archive that answered `read` would feed terminal entries back into
        loop logic through the back door."""
        return self._parse(kind, self._archive_dir(kind) / f"{obj_id}.md")

    def count_archived(self, kind: str) -> int:
        """How many archived files exist — filenames only, no parse. The
        overview surfaces want the number, not the objects."""
        d = self._archive_dir(kind)
        return sum(1 for _ in d.glob("*.md")) if d.is_dir() else 0

    # Convenience filters -------------------------------------------------

    def list_questions(self, state: str | None = None) -> list[TelosObject]:
        return self.list("question", **({"state": state} if state else {}))

    def list_hypotheses(self, status: str | None = None) -> list[TelosObject]:
        """Live hypotheses only. Terminal ones (untestable | expired) sit in
        soup/archive/ and never appear here — see `list_archived`."""
        return self.list("hypothesis", **({"status": status} if status else {}))

    def list_alarms(self, open_only: bool = True) -> list[TelosObject]:
        """open_only keeps the *live* alarms — open and acknowledged both.
        Acknowledgement is an operator note, not a resolution; only 'cleared'
        (the signature stopped holding) takes an alarm off the books."""
        alarms = self.list("alarm")
        if open_only:
            alarms = [a for a in alarms if a.get("state", "open") in LIVE_ALARM_STATES]
        return alarms

    # ------------------------------------------------------------------
    # Claims (humility layer, spec §6)
    # ------------------------------------------------------------------

    def commit_claim(
        self,
        text: str,
        epistemic_class: str,
        confidence: float,
        derived_from: list[str] | None = None,
        provenance_terminal: str = "readable",
        body: str = "",
    ) -> TelosObject:
        """Commit a claim with the class cap enforced. Chains terminating in
        model weights get terminal 'opaque' and keep their caps permanently."""
        cap = EPISTEMIC_CAPS.get(epistemic_class, EPISTEMIC_CAPS["self_report"])
        capped = min(max(0.0, float(confidence)), cap)
        obj = TelosObject(
            id=self.mint_id("claim"),
            kind="claim",
            meta={
                "text": text[:600],
                "epistemic_class": epistemic_class,
                "confidence": round(capped, 3),
                "confidence_cap": cap,
                "derived_from": list(derived_from or []),
                "provenance_terminal": provenance_terminal,
            },
            body=body,
        )
        self.write(obj)
        self.trace_append(
            "claim_commit",
            {"id": obj.id, "class": epistemic_class, "confidence": capped, "derived_from": derived_from or []},
        )
        return obj

    # ------------------------------------------------------------------
    # Trace ledger (spec §5.4): append-only JSONL, one file per UTC day.
    # The ONLY writer in the codebase. Nothing rewrites a written line.
    # ------------------------------------------------------------------

    def trace_path(self, day: str | None = None) -> Path:
        return self.root / "ledgers" / "trace" / f"{day or _today()}.jsonl"

    def trace_append(self, event_type: str, data: dict) -> None:
        """Append one event. Failure logs and returns — the trace must never
        be the reason a turn or a slow loop fails."""
        try:
            line = json.dumps(
                {"ts": _now_iso(), "epoch_ms": int(time.time() * 1000), "type": event_type, **data},
                ensure_ascii=False,
                default=str,
            )
            with self.trace_path().open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.warning("telos: trace append failed: %s", e)

    def trace_events(self, days: int = 7, types: set[str] | None = None) -> list[dict]:
        """Events from the last N daily files, oldest-first. Bad lines skipped."""
        from datetime import timedelta

        out: list[dict] = []
        now = datetime.now(timezone.utc)
        for i in range(days - 1, -1, -1):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            p = self.trace_path(day)
            if not p.is_file():
                continue
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if types is None or ev.get("type") in types:
                        out.append(ev)
            except OSError:
                continue
        return out

    # ------------------------------------------------------------------
    # Root + config provenance (spec §4.1, §6)
    # ------------------------------------------------------------------

    def ensure_root(self) -> TelosObject:
        """Seed g_root and the config-tier provenance record on first run.
        The root is a question with no satisfaction predicate; re-expression
        requires operator co-sign (spec: resign_requires operator)."""
        root = self.read("goal", "g_root")
        if root is not None:
            return root
        root = TelosObject(
            id="g_root",
            kind="goal",
            meta={
                "kind": "root",
                "text": settings.telos_root_text,
                "state": "active",
                "completable": False,
                "satisfaction_predicate": None,
                "resign_requires": "operator",
                "parent": None,
            },
            body=(
                "The root objective is a question, not a state. It has no "
                "satisfaction predicate — no observation closes it — and may only "
                "be re-expressed with operator co-sign. See spec §4.1 and §9: this "
                "is a representable stand-in; do not mistake the model for the thing modeled."
            ),
        )
        self.write(root)
        self._write_config_provenance()
        self.trace_append("root_seeded", {"text": settings.telos_root_text})
        return root


    def _write_config_provenance(self) -> None:
        """config/telos.yaml — the readable provenance tier (spec §6). The
        agent can inspect who installed its drive and what it is aimed at;
        the substrate tier (model weights) stays opaque by construction."""
        cfg = {
            "telos": {
                "root": {"text": settings.telos_root_text, "resign_requires": "operator"},
                "serendipity_budget": settings.telos_serendipity_budget,
                "soup_bands": {"near": 0.50, "mid": 0.30, "far": 0.20},
                "gate": {"eig_floor": settings.telos_eig_floor, "require_falsifier": True},
                "humility": {"self_report_cap": EPISTEMIC_CAPS["self_report"]},
                "entropy": {"novelty_floor": 0.20, "far_band_min": 0.10},
                "provenance": {
                    "installed_by": "operator",
                    "installed_at": _now_iso(),
                    "config_tier": "readable",
                    "substrate_tier": "opaque",
                },
            }
        }
        path = self.root / "config" / "telos.yaml"
        if not path.exists():
            path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # Runtime state (entropy control's band-mix actuation, spec §5.5)
    # ------------------------------------------------------------------

    def _state_path(self) -> Path:
        return self.root / "config" / "state.yaml"

    def get_state(self) -> dict:
        p = self._state_path()
        if p.is_file():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    return data
            except (OSError, yaml.YAMLError):
                pass
        return {}

    def set_state(self, **changes) -> dict:
        state = self.get_state()
        state.update(changes)
        self._state_path().write_text(yaml.safe_dump(state, sort_keys=True), encoding="utf-8")
        return state

    def band_mix(self) -> dict:
        """Current near/mid/far mix — defaults, shifted by Entropy Control."""
        state = self.get_state()
        mix = state.get("soup_bands") or {}
        near = float(mix.get("near", 0.50))
        mid = float(mix.get("mid", 0.30))
        far = float(mix.get("far", 0.20))
        total = near + mid + far
        if total <= 0:
            return {"near": 0.50, "mid": 0.30, "far": 0.20}
        return {"near": near / total, "mid": mid / total, "far": far / total}

    def serendipity_budget(self) -> float:
        state = self.get_state()
        try:
            v = float(state.get("serendipity_budget", settings.telos_serendipity_budget))
        except (TypeError, ValueError):
            v = settings.telos_serendipity_budget
        return min(max(v, 0.05), 0.5)

    # ------------------------------------------------------------------
    # Questions
    # ------------------------------------------------------------------

    def add_question(
        self,
        text: str,
        surprise: float = 0.5,
        derived_from: list[str] | None = None,
        parent_goal: str = "g_root",
        origin: str = "anomaly",
    ) -> TelosObject:
        obj = TelosObject(
            id=self.mint_id("question"),
            kind="question",
            meta={
                "text": text[:600],
                "surprise": round(min(max(float(surprise), 0.0), 1.0), 3),
                "state": "open",
                "derived_from": list(derived_from or []),
                "parent_goal": parent_goal,
                "origin": origin,  # anomaly | serendipity | gap_analysis | operator
                "spawned": [],
                "attempts": 0,
            },
        )
        self.write(obj)
        self.trace_append("question_minted", {"id": obj.id, "text": obj.get("text"), "origin": origin})
        return obj

    def question_is_duplicate(self, text: str, threshold: float = 0.85, questions: list | None = None) -> bool:
        """`questions` lets a caller that already scanned the corpus reuse it —
        the turn-end hook used to trigger up to six full directory scans."""
        from difflib import SequenceMatcher

        norm = text.lower().strip()
        for q in questions if questions is not None else self.list_questions():
            if SequenceMatcher(None, norm, str(q.get("text", "")).lower()).ratio() >= threshold:
                return True
        return False
