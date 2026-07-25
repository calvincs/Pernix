"""Pernix — Snooze: idle-time memory consolidation and optimization.

Runs during idle periods to:
1. Catch-up distillation for un-reviewed sessions
2. User insight extraction (profile facts, preferences, behavioral patterns)
3. Memory deduplication sweeps
4. Tag enrichment for sparse entries
5. FTS5 index reconciliation
6. Memory file splitting (rare, for bloated files)

Interruptible via cooperative cancellation. Uses background_model only.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from config import settings

logger = logging.getLogger("pernix.snooze")


class SnoozeRunner:
    """Background idle-time self-optimization."""

    def __init__(self):
        # Generation counter replaces asyncio.Event for cancel signalling.
        # Event.clear() at cycle start could swallow a cancel that fired during
        # the idle/activity pre-checks. Counters have no such window: if cancel
        # is requested at any point before _cycle_generation is captured,
        # _is_cancelled() returns True immediately.
        self._cancel_generation: int = 0  # bumped by request_cancel()
        self._cycle_generation: int = -1  # set to _cancel_generation at cycle start
        self._running = False
        self._stats = {
            "cycles": 0,
            "cycles_skipped": 0,
            "sessions_reviewed": 0,
            "entries_saved": 0,
            "entries_deduped": 0,
            "entries_enriched": 0,
            "last_cycle": None,
        }
        self._activity_since_last_cycle: bool = True  # first cycle always runs
        self._last_cycle_time: float = 0.0

    def request_cancel(self) -> None:
        """Signal Snooze to stop ASAP. Called when work arrives."""
        if self._running:
            self._cancel_generation += 1
            logger.debug("Snooze cancel requested (gen=%d)", self._cancel_generation)

    def notify_activity(self) -> None:
        """Signal that user/cron activity occurred, so next Snooze cycle should run."""
        self._activity_since_last_cycle = True

    def get_stats(self) -> dict:
        return {**self._stats, "running": self._running}

    # ------------------------------------------------------------------
    # Idle detection
    # ------------------------------------------------------------------

    def _is_idle(self) -> bool:
        """Check if the system is truly idle (relaxed gate)."""
        from sessions.manager import get_manager

        manager = get_manager()

        # 1. No active processing (v2 state: AWAITING_USER/AWAITING_WORKERS are
        # suspended-not-running, so they count as idle for snooze purposes)
        from sessions import state_v2 as _sv2

        _idle_v2 = (
            _sv2.SessionStateV2.IDLE_READY,
            _sv2.SessionStateV2.AWAITING_USER,
            _sv2.SessionStateV2.AWAITING_WORKERS,
        )
        # Snapshot once and reuse: a tool thread can insert into _sessions
        # (spawn_worker -> create_session) while this runs on the event loop,
        # and iterating the live dict raises "dictionary changed size during
        # iteration" — which would kill the snooze cycle outright.
        sessions = list(manager._sessions.values())

        for session in sessions:
            if _sv2._current_state(session) not in _idle_v2:
                return False

        # 2. No background tasks
        for session in sessions:
            if session.has_background_tasks:
                return False

        # 3. No cron jobs executing
        try:
            from db import models as db

            running_crons = db.list_cron_runs(limit=5)
            if any(r.get("status") == "running" for r in running_crons):
                return False
        except Exception:
            pass

        # 4. Cooldown elapsed (5 min since last user activity)
        cooldown = settings.snooze_cooldown_minutes * 60
        now = time.time()
        for session in sessions:
            if (now - session.last_activity_time) < cooldown:
                return False

        return True

    def _is_cancelled(self) -> bool:
        return self._cancel_generation != self._cycle_generation

    def _llm_available(self) -> bool:
        """Check if LLM semaphore is fully available (no contention)."""
        try:
            from core.llm.client import _get_semaphore_stats

            stats = _get_semaphore_stats()
            return stats["available"] == stats["capacity"]
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    # Minimum interval between snooze cycles when nothing has changed.
    # Active-work gate below handles the "don't step on a running session"
    # concern; this cadence just keeps snooze from burning cycles on an
    # idle-but-unchanged system.
    _MIN_CYCLE_INTERVAL_SEC = 900  # 15 min

    async def run_cycle(self) -> None:
        """Run one Snooze cycle. Called by maintenance heartbeat."""
        if not settings.snooze_enabled:
            return

        # Explicit active-work gate. Do NOT clear _activity_since_last_cycle
        # here — we want snooze to run at the next opportunity once things
        # quiesce, carrying forward the pending-work flag.
        try:
            from sessions.manager import get_manager

            if get_manager().has_active_work():
                self._stats["cycles_skipped"] += 1
                logger.debug("Snooze: skipping (active session or worker)")
                return
        except Exception:
            # Don't let a manager import failure kill snooze.
            pass

        if not self._is_idle():
            return

        # Skip if no activity since last cycle and last run was recent.
        if not self._activity_since_last_cycle:
            if self._last_cycle_time and (time.time() - self._last_cycle_time < self._MIN_CYCLE_INTERVAL_SEC):
                self._stats["cycles_skipped"] += 1
                logger.debug("Snooze: no activity since last cycle, skipping")
                return

        # Capture current cancel generation. Any request_cancel() that fired
        # before this point will make _is_cancelled() return True immediately.
        self._cycle_generation = self._cancel_generation
        self._running = True
        logger.info("Snooze cycle starting")

        from core.events import get_event_bus

        bus = get_event_bus()
        _start = time.time()
        bus.emit({"type": "snooze.start", "activity": "cycle"})

        try:
            await asyncio.wait_for(
                self._do_cycle(),
                timeout=settings.snooze_max_cycle_seconds,
            )
        except asyncio.TimeoutError:
            logger.info("Snooze cycle hit time limit (%ds)", settings.snooze_max_cycle_seconds)
        except asyncio.CancelledError:
            # Re-raise. Swallowing a cancel here meant that at shutdown,
            # maint.stop() -> task.cancel() landed inside the cycle, was
            # absorbed, and the maintenance tick carried on into the WAL
            # checkpoint and vacuum branches while shutdown waited on it.
            # The finally below still runs, so cycle bookkeeping is intact.
            logger.debug("Snooze cycle cancelled")
            raise
        except Exception as e:
            logger.error("Snooze cycle error: %s", e, exc_info=True)
        finally:
            self._running = False
            self._stats["cycles"] += 1
            self._stats["last_cycle"] = datetime.now(timezone.utc).isoformat()
            self._activity_since_last_cycle = False
            self._last_cycle_time = time.time()
            duration_ms = int((time.time() - _start) * 1000)
            bus.emit({"type": "snooze.done", "duration_ms": duration_ms, "stats": {**self._stats}})
            logger.info("Snooze cycle complete (stats: %s)", self._stats)

    async def _do_cycle(self) -> None:
        """Execute activities in priority order."""
        from core.events import get_event_bus

        bus = get_event_bus()
        did_llm = False
        # Separate budget for file-org activities (split, stale prune) so that
        # content-creation LLM calls (distill, insights, proposals) don't starve them.
        did_maintenance_llm = False

        # Activity 1: Catch-up distillation (max 1 LLM call)
        if not self._is_cancelled() and not did_llm:
            can_llm = self._llm_available() and bool(settings.background_model or settings.llm_model)
            if can_llm:
                bus.emit(
                    {"type": "snooze.activity", "activity": "distill", "detail": "Catching up on un-reviewed sessions"}
                )
                reviewed = await self._catchup_distill()
                if reviewed:
                    did_llm = True

        # Activity 2: User insight extraction (LLM, if distill didn't use it)
        if not self._is_cancelled() and not did_llm:
            can_llm = self._llm_available() and bool(settings.background_model or settings.llm_model)
            if can_llm:
                bus.emit(
                    {
                        "type": "snooze.activity",
                        "activity": "user_insights",
                        "detail": "Extracting user profile insights from conversations",
                    }
                )
                extracted = await self._extract_user_insights()
                if extracted:
                    did_llm = True

        # Activity 2b: Skill-improvement proposals + lessons from session reflects.
        # Mirrors workflow_reflect's self-improvement loop for non-workflow sessions.
        # LLM-gated and mutually exclusive with distill/insights via did_llm.
        if not self._is_cancelled() and not did_llm:
            can_llm = self._llm_available() and bool(settings.background_model or settings.llm_model)
            if can_llm:
                bus.emit(
                    {
                        "type": "snooze.activity",
                        "activity": "propose_skill_improvements",
                        "detail": "Extracting skill proposals + lessons from session reflects",
                    }
                )
                used_llm = await self._propose_skill_improvements()
                if used_llm:
                    did_llm = True

        # Activity 3: Dedup sweep (no LLM)
        if not self._is_cancelled():
            bus.emit(
                {"type": "snooze.activity", "activity": "dedup", "detail": "Checking for duplicate memory entries"}
            )
            await self._dedup_sweep()

        # Activity 3b: Cross-file consolidation (trivial=no LLM, ambiguous=LLM)
        if not self._is_cancelled():
            bus.emit(
                {
                    "type": "snooze.activity",
                    "activity": "consolidate",
                    "detail": "Consolidating overlapping memory files",
                }
            )
            consolidate_used_llm = await self._consolidate_files(did_llm)
            if consolidate_used_llm:
                did_llm = True

        # Activity 3c: Entry re-routing (fix entries in the wrong file)
        if not self._is_cancelled():
            bus.emit(
                {
                    "type": "snooze.activity",
                    "activity": "reroute",
                    "detail": "Re-routing misplaced memory entries to correct files",
                }
            )
            reroute_used_llm = await self._reroute_misplaced_entries(did_llm)
            if reroute_used_llm:
                did_llm = True

        # Activity 4: Tag enrichment (no LLM)
        if not self._is_cancelled():
            bus.emit({"type": "snooze.activity", "activity": "enrich_tags", "detail": "Enriching memory entry tags"})
            await self._enrich_tags()

        # Activity 5: Index reconciliation (no LLM)
        if not self._is_cancelled():
            bus.emit({"type": "snooze.activity", "activity": "reconcile", "detail": "Reconciling memory index"})
            await self._reconcile_index()

        # Activity 6: File splitting (LLM, maintenance budget — independent of did_llm)
        if not self._is_cancelled() and not did_maintenance_llm:
            can_llm = self._llm_available() and bool(settings.background_model or settings.llm_model)
            if can_llm:
                bus.emit({"type": "snooze.activity", "activity": "split", "detail": "Splitting large memory files"})
                split_used_llm = await self._split_file()
                if split_used_llm:
                    did_maintenance_llm = True

        # Activity 7: Cron cleanup (no LLM)
        if not self._is_cancelled():
            await self._cleanup_cron(bus)

        # Activity 8: Staleness pruning (LLM, maintenance budget — runs even when distill used LLM)
        if not self._is_cancelled() and not did_maintenance_llm:
            can_llm = self._llm_available() and bool(settings.background_model or settings.llm_model)
            if can_llm:
                bus.emit(
                    {
                        "type": "snooze.activity",
                        "activity": "stale_prune",
                        "detail": "Pruning stale low-recall memory entries",
                    }
                )
                await self._prune_stale_entries()

        # Activity 9: Skill cooccurrence update (no LLM)
        if not self._is_cancelled():
            bus.emit(
                {
                    "type": "snooze.activity",
                    "activity": "skill_cooccurrence",
                    "detail": "Updating skill co-occurrence map from memory",
                }
            )
            await self._update_skill_cooccurrence()

        # Activity 10: Synthesize post-mortems into tool/skill performance counters (no LLM)
        if not self._is_cancelled():
            bus.emit(
                {
                    "type": "snooze.activity",
                    "activity": "synthesize_signals",
                    "detail": "Synthesizing post-mortems into scout signals",
                }
            )
            await self._synthesize_signals()

        # Activity 11: Post-mortem TTL cleanup (no LLM)
        if not self._is_cancelled():
            bus.emit(
                {
                    "type": "snooze.activity",
                    "activity": "cleanup_post_mortems",
                    "detail": "Pruning old synthesized post-mortems",
                }
            )
            await self._cleanup_post_mortems()

        # Activity 12: Workflow run directory cleanup (no LLM)
        if not self._is_cancelled():
            bus.emit(
                {
                    "type": "snooze.activity",
                    "activity": "cleanup_workflow_runs",
                    "detail": "Pruning old workflow run directories",
                }
            )
            await self._cleanup_workflow_runs()

        # Activity 13: Refine pass — broader-gate sibling of Activity 2b.
        # Runs independent of did_llm: refine has its own budget, bounded to
        # one session per cycle. Coexists with 2b — a session with an
        # actionable reflect verdict may produce both a narrow proposal (2b)
        # and a broader refine pass (13). Watermark differs (refined:{sid}
        # vs proposal_reviewed:{sid}) so the two passes don't collide.
        if not self._is_cancelled():
            can_llm = self._llm_available() and bool(settings.background_model or settings.llm_model)
            if can_llm:
                bus.emit(
                    {
                        "type": "snooze.activity",
                        "activity": "refine",
                        "detail": "Crystallizing skill/memory updates from an idle session",
                    }
                )
                await self._refine_one_session()

    # ------------------------------------------------------------------
    # Activity 1: Catch-up distillation
    # ------------------------------------------------------------------

    async def _catchup_distill(self) -> bool:
        """Review one un-distilled session. Returns True if work was done."""
        from db import models as db

        sessions = db.get_unreviewed_sessions(
            min_age_minutes=settings.snooze_cooldown_minutes * 2,
            limit=1,
        )
        if not sessions:
            return False

        session = sessions[0]
        sid = session["id"]

        # Skip distillation if user already saved entries manually in this session.
        # Manual saves are tracked by the remember() tool via snooze_state.
        manual_save = db.get_snooze_state(f"manual_save:{sid}")
        if manual_save:
            logger.info("Snooze: session %s has manual saves, skipping distillation", sid)
            db.mark_session_reviewed(sid)
            return False

        logger.info("Snooze: distilling session %s (%s)", sid, session.get("title", "?"))

        try:
            messages = db.get_messages(sid)
            # Filter to substantive messages
            substantive = [m for m in messages if m["role"] in ("user", "assistant") and m.get("content")]
            if len(substantive) < 4:
                db.mark_session_reviewed(sid)
                return False

            from core.memory.distill import distill_session

            await distill_session(
                session_id=sid,
                title=session.get("title", "Untitled"),
                messages=messages,
                session_type=session.get("session_type", "normal"),
            )
            db.mark_session_reviewed(sid)
            self._stats["sessions_reviewed"] += 1
            logger.info("Snooze: distilled session %s", sid)
            return True

        except Exception as e:
            logger.warning("Snooze: distillation failed for %s: %s", sid, e)
            # Still mark as reviewed to avoid retrying a broken session forever
            db.mark_session_reviewed(sid)
            return False

    # ------------------------------------------------------------------
    # Activity 2: User insight extraction
    # ------------------------------------------------------------------

    _USER_INSIGHTS_PROMPT = """You are a user profiling agent. Analyze this conversation and extract facts about the USER — not about the technical work itself (that's handled separately).

Extract ONLY information that would help a future AI assistant understand and serve this user better. Output a JSON array of entries, each:
{
  "tags": "comma,separated,keywords",
  "weight": "high|normal",
  "content": "Self-contained fact about the user"
}

Categories to look for (extract only what's actually present — do NOT infer or guess):

IDENTITY & DEMOGRAPHICS: name, age, birthday, gender, location, timezone, language, occupation, role, employer, team
EXPERTISE & KNOWLEDGE: technical skills, domains of experience, experience level, certifications, education background
PREFERENCES & STYLE: preferred tools, languages, frameworks, communication style, verbosity preference, work habits
LIKES & DISLIKES: stated preferences, things they explicitly enjoy or avoid, aesthetic tastes, workflow preferences
GOALS & CONTEXT: what they're working toward, project context, deadlines, constraints, team dynamics
BEHAVIORAL PATTERNS: what approaches worked well for them, what frustrated them, how they like to collaborate with AI, feedback they gave about responses

RULES:
- Only extract what the user explicitly stated or clearly demonstrated — never guess demographics
- Each fact must be self-contained (understandable without the conversation)
- Use "weight": "high" for core identity facts (name, role, location) and strong stated preferences
- Use "weight": "normal" for contextual observations and softer patterns
- If the conversation reveals nothing about the user personally, respond with just: SKIP
- Do NOT extract technical findings, code decisions, or project architecture — those are handled by distillation
- Be concise: each content field should be 1-2 sentences max

Output valid JSON only. No markdown fences. /no_think"""

    async def _extract_user_insights(self) -> bool:
        """Extract user profile facts from one already-distilled session.

        Returns True if LLM was used.
        """
        from db import models as db

        # Find sessions that have been distilled but not yet profiled
        sessions = db.get_unreviewed_sessions(
            min_age_minutes=settings.snooze_cooldown_minutes * 2,
            limit=5,
        )
        # get_unreviewed_sessions returns sessions where snooze_reviewed_at IS NULL.
        # We need sessions that HAVE been distilled. Query directly.
        with db.connect_sessions() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=settings.snooze_cooldown_minutes * 2)).isoformat()
            rows = conn.execute(
                """SELECT s.* FROM sessions s
                   WHERE s.snooze_reviewed_at IS NOT NULL
                     AND s.state = 'idle'
                     AND s.updated_at < ?
                     AND s.session_type != 'worker'
                     AND (
                         SELECT COUNT(*) FROM messages m
                         WHERE m.session_id = s.id
                           AND m.role = 'user'
                           AND m.content != ''
                     ) >= 2
                   ORDER BY s.updated_at ASC
                   LIMIT 5""",
                (cutoff,),
            ).fetchall()

        # Filter out sessions already profiled (tracked via snooze_state)
        candidates = []
        for row in rows:
            sid = row["id"]
            if not db.get_snooze_state(f"profiled:{sid}"):
                candidates.append(dict(row))
        if not candidates:
            return False

        session = candidates[0]
        sid = session["id"]
        logger.info("Snooze: extracting user insights from session %s (%s)", sid, session.get("title", "?"))

        try:
            messages = db.get_messages(sid)
            # Build transcript focused on user messages (with assistant context)
            transcript_lines = [f"Session: {session.get('title', 'Untitled')}"]
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user" and content:
                    transcript_lines.append(f"[USER] {content[:1000]}")
                elif role == "assistant" and content:
                    transcript_lines.append(f"[ASSISTANT] {content[:400]}")
            transcript = "\n".join(transcript_lines)

            if len(transcript) < 200:
                db.set_snooze_state(f"profiled:{sid}", str(time.time()))
                return False

            from core.llm.client import get_llm_client

            client = get_llm_client()
            model = settings.background_model or settings.llm_model

            try:
                response = await client.chat(
                    messages=[
                        {"role": "system", "content": self._USER_INSIGHTS_PROMPT},
                        {"role": "user", "content": transcript[:15000]},
                    ],
                    model=model,
                    max_tokens=1500,
                )
            except Exception as e:
                logger.warning("Snooze: user insight LLM call failed for %s: %s", sid, e)
                db.set_snooze_state(f"profiled:{sid}", str(time.time()))
                return True
            text = response.content.strip()

            db.set_snooze_state(f"profiled:{sid}", str(time.time()))

            if text.upper() == "SKIP":
                logger.debug("Snooze: no user insights for session %s", sid)
                return True

            # Parse entries
            entries = self._parse_insight_entries(text)
            if not entries:
                return True

            # Save with dedup
            from core.memory.store import get_memory_store

            store = get_memory_store()
            if not store:
                return True

            saved = 0
            for entry in entries:
                content = entry.get("content", "")
                if not content:
                    continue

                # Multi-signal dedup against existing memories
                if store.is_duplicate(content):
                    continue

                tags = entry.get("tags", "")
                tags = f"user,profile,{tags}" if tags else "user,profile"
                tags += f",{time.strftime('%Y-%m-%d')}"

                await asyncio.to_thread(
                    store.add_entry,
                    content=content,
                    file_name="user.profile",
                    entry_type="profile",
                    tags=tags,
                    weight=entry.get("weight", "normal"),
                    source="snooze",
                )
                saved += 1
                await asyncio.sleep(0.1)

            if saved:
                self._stats["entries_saved"] += saved
                logger.info("Snooze: extracted %d user insight(s) from session %s", saved, sid)
            return True

        except Exception as e:
            logger.warning("Snooze: user insight extraction failed for %s: %s", sid, e)
            db.set_snooze_state(f"profiled:{sid}", str(time.time()))
            return False

    @staticmethod
    def _parse_insight_entries(text: str) -> list[dict]:
        """Parse JSON array from LLM response."""
        import json

        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return [data]
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            logger.debug("Failed to parse user insights JSON: %s", text[:200])
        return []

    _REROUTE_PROMPT = """You are a memory file auditor. Review these memory entries that may be stored in the wrong file.

EXISTING MEMORY FILES:
{file_catalog}

ENTRIES TO REVIEW:
{entry_list}

For each entry, decide: keep in the current file, move to an existing file, or group with others into a new file.

ROUTING GUIDANCE:
- Personal info about the user (name, location, employer, hardware, preferences) → user identity/profile file
- System design, components, agent loop, workers, tool schemas, deployment → Pernix config/architecture file
- Operational lessons, mistakes, recovery patterns, critical gotchas → lessons or debugging file
- Tool usage patterns, code workflows, command recipes → tools or patterns file
- External findings, third-party analysis → research file
- Skill-specific content → matching skill file only; general patterns go to lessons/tools

FILE CREATION RULES:
- PREFER existing files whenever a reasonable match exists.
- You MAY suggest a new file name ONLY if 2 or more entries in this batch share a coherent
  topic that no existing file covers. A single orphan entry does not justify a new file —
  keep it or move it to the closest existing file instead.
- New file names must be dot-separated lowercase: e.g. "pernix.vision", "user.hardware", "pernix.auth".
- Keep new names short (2-3 segments). Do not create near-duplicates of existing files.

Output a JSON array — one entry per reviewed item:
[{{"epoch": <number>, "action": "keep|move", "target_file": "filename", "reason": "brief reason"}}]

For "keep", set target_file to the current file name.
Output valid JSON only. No markdown fences. /no_think"""

    # ------------------------------------------------------------------
    # Activity 2b: Skill-improvement proposals + lessons from session reflects
    # ------------------------------------------------------------------

    async def _propose_skill_improvements(self) -> bool:
        """Run snooze_reflect on one un-improvement-reviewed session.

        Returns True if an LLM call was made (so the caller can flip did_llm).
        Watermarked via snooze_state['proposal_reviewed:{sid}'] so a session
        is processed at most once. Mark-on-failure pattern matches
        _catchup_distill — a broken session never triggers a retry storm.
        """
        from db import models as db

        sessions = db.get_unproposed_sessions(
            min_age_minutes=settings.snooze_cooldown_minutes * 2,
            limit=1,
        )
        if not sessions:
            return False

        session = sessions[0]
        sid = session["id"]

        try:
            from core.snooze_reflect import run_for_session

            stats = await run_for_session(sid)
            self._stats.setdefault("proposals_saved", 0)
            self._stats.setdefault("lessons_saved", 0)
            self._stats["proposals_saved"] += stats.get("proposals_saved", 0)
            self._stats["lessons_saved"] += stats.get("lessons_saved", 0)
            llm_used = stats.get("skipped_reason") not in (
                "session_not_found",
                "worker_session",
                "no_reflect_verdict",
                "low_reflect_confidence",
                "no_model_configured",
            ) and not (stats.get("skipped_reason") or "").startswith("non_actionable_cause")
            db.set_snooze_state(f"proposal_reviewed:{sid}", "1")
            return bool(llm_used)
        except Exception as e:
            logger.warning("Snooze: skill-improvement reflect failed for %s: %s", sid, e)
            db.set_snooze_state(f"proposal_reviewed:{sid}", "1")
            return False

    # ------------------------------------------------------------------
    # Activity 13: Whole-session refine (tail-end of snooze cycle)
    # ------------------------------------------------------------------

    async def _refine_one_session(self) -> bool:
        """Run a broader-gate refine pass on one idle session.

        Selects via :func:`db.get_unrefined_sessions` (10-min idle floor,
        watermark ``refined:{sid}``). Stamps the watermark unconditionally
        after the call so a session that produced nothing actionable, or
        one whose LLM call failed, is never retried — matches the
        mark-on-failure pattern used by ``_catchup_distill`` and
        ``_propose_skill_improvements``.

        Returns True if the LLM was invoked (so stats reflect cycle work),
        False otherwise. Snooze does not gate any subsequent activity on
        this return, but the bool keeps the call site uniform.
        """
        from db import models as db

        # 2× snooze_cooldown_minutes mirrors the floor used by
        # _propose_skill_improvements (Activity 2b) — keeps both passes
        # operating on the same notion of "idle long enough."
        sessions = db.get_unrefined_sessions(
            min_idle_minutes=settings.snooze_cooldown_minutes * 2,
            limit=1,
        )
        if not sessions:
            return False

        session = sessions[0]
        sid = session["id"]

        if self._is_cancelled():
            return False

        try:
            from core.refine import run_for_session

            stats = await run_for_session(sid)
            self._stats.setdefault("refine_proposals_saved", 0)
            self._stats.setdefault("refine_lessons_saved", 0)
            self._stats.setdefault("refine_nothing_actionable", 0)
            self._stats["refine_proposals_saved"] += stats.get("proposals_saved", 0)
            self._stats["refine_lessons_saved"] += stats.get("lessons_saved", 0)
            if stats.get("nothing_actionable"):
                self._stats["refine_nothing_actionable"] += 1
            llm_used = stats.get("skipped_reason") not in (
                "session_not_found",
                "worker_session",
                "no_messages",
                "insufficient_exchange",
                "no_model_configured",
            )
            db.set_snooze_state(f"refined:{sid}", datetime.now(timezone.utc).isoformat())
            return bool(llm_used)
        except Exception as e:
            logger.warning("Snooze: refine pass failed for %s: %s", sid, e)
            db.set_snooze_state(f"refined:{sid}", datetime.now(timezone.utc).isoformat())
            return False

    # ------------------------------------------------------------------
    # Activity 3: Memory deduplication sweep
    # ------------------------------------------------------------------

    async def _dedup_sweep(self) -> None:
        """Scan one memory file for near-duplicate entries."""
        from core.memory.format import parse_entries_from_markdown
        from core.memory.store import get_memory_store
        from db import models as db

        store = get_memory_store()
        if not store:
            return

        # Find a file due for dedup
        interval_seconds = settings.snooze_dedup_interval_days * 86400
        files = await asyncio.to_thread(store.list_files)

        target_file = None
        for f in files:
            if f.entry_count < 5:
                continue
            key = f"dedup_{f.name}"
            last_sweep = db.get_snooze_state(key)
            if last_sweep:
                try:
                    last_dt = datetime.fromisoformat(last_sweep)
                    if (datetime.now(timezone.utc) - last_dt).total_seconds() < interval_seconds:
                        continue
                except ValueError:
                    pass
            target_file = f
            break

        if not target_file:
            return

        if self._is_cancelled():
            return

        logger.info("Snooze: dedup sweep on %s (%d entries)", target_file.name, target_file.entry_count)

        # Parse entries from markdown
        md_content = await asyncio.to_thread(store.read_file, target_file.name)
        if not md_content:
            return

        entries = parse_entries_from_markdown(target_file.name, md_content)
        if len(entries) < 2:
            db.set_snooze_state(f"dedup_{target_file.name}", datetime.now(timezone.utc).isoformat())
            return

        # Pairwise similarity check.
        # Off-loop (asyncio.to_thread) so the event loop stays responsive even
        # on large files, and use SequenceMatcher's O(1) real_quick_ratio() and
        # O(N+M) quick_ratio() as upper-bound prescreens — pairs that can't
        # reach 0.82 are skipped without ever computing the full O(N·M) ratio.
        def _pairwise_dedup() -> set[int]:
            import re as _re

            def _tokens(s: str) -> set[str]:
                return set(_re.findall(r"\w+", s.lower()))

            archived: set[int] = set()
            for i in range(len(entries)):
                if self._is_cancelled():
                    break
                if entries[i].epoch in archived:
                    continue
                for j in range(i + 1, len(entries)):
                    if entries[j].epoch in archived:
                        continue
                    sm = SequenceMatcher(None, entries[i].content, entries[j].content)
                    if sm.real_quick_ratio() < 0.82 or sm.quick_ratio() < 0.82:
                        continue
                    sim = sm.ratio()
                    if sim > 0.82:
                        to_archive = entries[j] if len(entries[j].content) <= len(entries[i].content) else entries[i]
                        to_keep = entries[i] if to_archive is entries[j] else entries[j]
                        # Ratio alone is not enough: structured facts that
                        # differ only in a key value ("prod key X / :8090" vs
                        # "dev key Y / :8091") score ~0.9 and one would be
                        # silently lost forever. Only archive when the dropped
                        # entry carries NO token absent from the kept one —
                        # i.e. archiving loses no unique information. Pure
                        # rephrasings with novel words stay (false negatives
                        # are wasteful; false positives destroy facts).
                        if not _tokens(to_archive.content) <= _tokens(to_keep.content):
                            continue
                        archived.add(to_archive.epoch)
                        logger.debug("Snooze: archiving duplicate (epoch=%d, sim=%.2f)", to_archive.epoch, sim)
            return archived

        archived_epochs = await asyncio.to_thread(_pairwise_dedup)

        if archived_epochs:
            # Markdown archive-tag + FTS removal as one uncancellable unit.
            await asyncio.to_thread(self._archive_entries, store, target_file.name, archived_epochs)
            self._stats["entries_deduped"] += len(archived_epochs)
            logger.info("Snooze: archived %d duplicates in %s", len(archived_epochs), target_file.name)

        db.set_snooze_state(f"dedup_{target_file.name}", datetime.now(timezone.utc).isoformat())

    def _archive_entries(self, store, file_name: str, epochs: set[int]) -> None:
        """Archive entries in markdown AND drop them from the FTS index.

        These two halves were always invoked back-to-back but as separate
        synchronous calls from async code. That ran them on the event loop
        (blocking every session's SSE) and left a window between them: a
        cancel or crash landing in the middle archived the markdown while the
        index still served the entry, so recall returned rows whose bodies
        were tagged archived. Snooze is the only writer that splits an entry
        across both stores, so it is the only place that drift originates.

        Callers dispatch this via asyncio.to_thread. to_thread cannot be
        cancelled, so once started both halves run to completion — the
        pairing is atomic with respect to cancellation, which is what closes
        the window rather than merely narrowing it.
        """
        self._archive_entries_in_file(store, file_name, epochs)
        self._remove_from_index(store, file_name, epochs)

    def _archive_entries_in_file(self, store, file_name: str, epochs: set[int]) -> None:
        """Add <!-- @archived: true --> tag to entries in markdown file.

        Prefer _archive_entries() — calling this without the index removal
        leaves markdown and FTS disagreeing.
        """
        import fcntl

        md_path = store._dir / f"{file_name}.md"
        if not md_path.exists():
            return

        with store._lock:
            content = md_path.read_text()
            for epoch in epochs:
                # Find the epoch comment and add archived tag after it
                pattern = f"<!-- @epoch: {epoch} -->"
                if pattern in content:
                    content = content.replace(
                        pattern,
                        f"{pattern}\n<!-- @archived: true -->",
                    )
            with open(md_path, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(content)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _remove_from_index(self, store, file_name: str, epochs: set[int]) -> None:
        """Remove archived entries from FTS5 index and clean up associated hit records."""
        conn = store._connect()
        try:
            for epoch in epochs:
                conn.execute(
                    "DELETE FROM memory_fts WHERE file_name = ? AND epoch = ?",
                    (file_name, str(epoch)),
                )
                # Also remove any hit-count records for this entry so memory_hits
                # doesn't accumulate orphan rows for epochs no longer in FTS5.
                conn.execute(
                    "DELETE FROM memory_hits WHERE file_name = ? AND epoch = ?",
                    (file_name, str(epoch)),
                )
            # Recount from FTS5 as the authoritative source
            remaining = conn.execute(
                "SELECT COUNT(*) as cnt FROM memory_fts WHERE file_name = ?",
                (file_name,),
            ).fetchone()
            if remaining:
                conn.execute(
                    "UPDATE memory_files SET entry_count = ?, updated_at = ? WHERE name = ?",
                    (remaining["cnt"], int(time.time()), file_name),
                )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Activity 3b: Cross-file consolidation
    # ------------------------------------------------------------------

    async def _consolidate_files(self, did_llm_already: bool) -> bool:
        """Consolidate overlapping memory files. Returns True if LLM was used."""
        from core.memory.consolidate import (
            build_llm_merge_prompt,
            build_signatures,
            execute_merge,
            find_clusters,
            parse_llm_merge_response,
            plan_trivial_merge,
            prioritize_clusters,
        )
        from core.memory.store import get_memory_store
        from db import models as db

        store = get_memory_store()
        if not store:
            return False

        # Rate limit: check interval
        interval_seconds = settings.snooze_consolidation_interval_hours * 3600
        last = db.get_snooze_state("last_consolidation_scan")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < interval_seconds:
                    return False
            except ValueError:
                pass

        if self._is_cancelled():
            return False

        # Phase 1: Build signatures and find clusters (no LLM).
        # The pairwise SequenceMatcher in find_clusters is CPU-heavy on
        # realistic stores — push it onto a worker thread so the asyncio
        # loop stays responsive (HTTP, SSE heartbeats, snooze timeout).
        signatures = await asyncio.to_thread(build_signatures, store)
        if len(signatures) < 2:
            db.set_snooze_state("last_consolidation_scan", datetime.now(timezone.utc).isoformat())
            return False

        sig_map = {s.name: s for s in signatures}
        clusters = await asyncio.to_thread(find_clusters, signatures, None, self._is_cancelled)

        if not clusters:
            db.set_snooze_state("last_consolidation_scan", datetime.now(timezone.utc).isoformat())
            return False

        clusters = prioritize_clusters(clusters, sig_map)

        if self._is_cancelled():
            return False

        # Phase 2: Process ONE cluster per cycle
        cluster = clusters[0]
        logger.info("Snooze: consolidating cluster %s (%d files)", cluster, len(cluster))

        used_llm = False

        # Try trivial merge first (no LLM). Also CPU-heavy when a cluster
        # has many entries — same offload reasoning as Phase 1.
        decision = await asyncio.to_thread(plan_trivial_merge, cluster, store)

        if decision is None and not did_llm_already:
            # Need LLM for ambiguous merge
            can_llm = self._llm_available() and bool(settings.background_model or settings.llm_model)
            if can_llm:
                prompt = build_llm_merge_prompt(cluster, store)
                try:
                    from core.llm.client import get_llm_client

                    client = get_llm_client()
                    model = settings.background_model or settings.llm_model
                    response = await client.chat(
                        messages=[
                            {"role": "system", "content": "You are a memory consolidation agent."},
                            {"role": "user", "content": prompt},
                        ],
                        model=model,
                        max_tokens=2000,
                    )
                    decision = parse_llm_merge_response(response.content.strip(), cluster)
                    used_llm = True
                except Exception as e:
                    logger.warning("Snooze: consolidation LLM call failed: %s", e)

        if decision:
            result = await asyncio.to_thread(execute_merge, store, decision)
            self._stats.setdefault("files_consolidated", 0)
            self._stats["files_consolidated"] += len(decision.source_files)
            logger.info(
                "Snooze: consolidated %d files into %s (%s)",
                len(decision.source_files),
                decision.target_file,
                decision.strategy,
            )

        db.set_snooze_state("last_consolidation_scan", datetime.now(timezone.utc).isoformat())
        return used_llm

    # ------------------------------------------------------------------
    # Activity 3c: Entry re-routing
    # ------------------------------------------------------------------

    async def _reroute_misplaced_entries(self, did_llm_already: bool) -> bool:
        """Scan memory files for entries that belong in a different file and move them.

        Two passes:
        1. No-LLM: type-consistency check + tag/keyword affinity scoring.
           Clear mismatches (current file score=0, best other score≥1) are moved immediately.
        2. LLM (if available and not used this cycle): medium-confidence candidates
           are reviewed against the full file catalog and routing rules.

        Returns True if the LLM was used.
        """
        import json as _json

        from core.memory.format import parse_entries_from_markdown
        from core.memory.ingest import _build_file_catalog
        from core.memory.store import NAMESPACE_KEYWORDS, get_memory_store
        from db import models as db

        store = get_memory_store()
        if not store:
            return False

        # Rate limit: share the consolidation interval so the two never run together
        interval_seconds = settings.snooze_consolidation_interval_hours * 3600
        last = db.get_snooze_state("last_reroute_scan")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < interval_seconds:
                    return False
            except ValueError:
                pass

        if self._is_cancelled():
            return False

        files = await asyncio.to_thread(store.list_files)
        if len(files) < 2:
            db.set_snooze_state("last_reroute_scan", datetime.now(timezone.utc).isoformat())
            return False

        # Build per-file keyword sets for scoring
        # Combines: file metadata keywords + file-name segments + NAMESPACE_KEYWORDS
        file_keywords: dict[str, set[str]] = {}
        for f in files:
            kws: set[str] = set()
            kws.update(kw.lower().strip() for kw in f.keywords if len(kw.strip()) > 2)
            kws.update(part for part in re.split(r"[._-]", f.name.lower()) if len(part) > 2)
            for ns, ns_kws in NAMESPACE_KEYWORDS.items():
                # Match namespace to file if name is equal or shares the first segment
                ns_root = ns.split(".")[0]
                file_root = f.name.split(".")[0]
                if f.name == ns or ns_root == file_root:
                    kws.update(ns_kws)
            file_keywords[f.name] = kws

        one_day_ago = int(time.time()) - 86400

        # The scoring loop is O(entries × files × keywords) and reads every
        # markdown file from disk — push it onto a worker thread so the event
        # loop stays responsive.  The inner function captures already-computed
        # locals (files, store, file_keywords, one_day_ago) and accepts a
        # cancel_check so snooze can bail early when work arrives.
        def _scan_for_candidates() -> tuple[list[dict], list[dict]]:
            from core.memory.format import parse_entries_from_markdown as _parse

            high: list[dict] = []
            medium: list[dict] = []
            for mem_file in files:
                if self._is_cancelled():
                    break
                if mem_file.entry_count < 2:
                    continue

                md_content = store.read_file(mem_file.name)
                if not md_content:
                    continue

                entries = _parse(mem_file.name, md_content)
                for entry in entries:
                    if self._is_cancelled():
                        break
                    if entry.epoch > one_day_ago:
                        continue

                    # Check 1: type-file consistency
                    if entry.entry_type == "profile" and mem_file.name != "user.profile":
                        high.append(
                            {
                                "entry": entry,
                                "src_file": mem_file.name,
                                "target_file": "user.profile",
                                "confidence": "high",
                                "reason": "profile type outside user.profile",
                            }
                        )
                        continue

                    # Check 2: tag/keyword affinity scoring
                    tag_str = " ".join(entry.tags).lower()
                    content_lower = entry.content.lower()

                    scores: dict[str, float] = {}
                    for fname, fkws in file_keywords.items():
                        tag_hits = sum(2.0 for kw in fkws if kw in tag_str)
                        content_hits = sum(0.5 for kw in fkws if kw in content_lower)
                        scores[fname] = tag_hits + content_hits

                    current_score = scores.get(mem_file.name, 0.0)
                    other = {k: v for k, v in scores.items() if k != mem_file.name}
                    if not other:
                        continue
                    best_other = max(other, key=other.get)
                    best_score = other[best_other]

                    if best_score < 1.0:
                        continue

                    if current_score == 0.0 and best_score >= 1.0:
                        medium.append(
                            {
                                "entry": entry,
                                "src_file": mem_file.name,
                                "target_file": best_other,
                                "confidence": "medium",
                                "reason": (
                                    f"no keyword affinity with {mem_file.name}; "
                                    f"score {best_score:.1f} for {best_other}"
                                ),
                            }
                        )
            return high, medium

        high_conf, medium_conf = await asyncio.to_thread(_scan_for_candidates)

        if not high_conf and not medium_conf:
            db.set_snooze_state("last_reroute_scan", datetime.now(timezone.utc).isoformat())
            return False

        used_llm = False
        rerouted = 0

        # ── Pass 1: high-confidence reroutes (no LLM) ───────────────────
        for item in high_conf:
            if self._is_cancelled():
                break
            entry = item["entry"]
            src, dst = item["src_file"], item["target_file"]
            try:
                moved = store.move_entries(src, dst, [entry.epoch])
                if moved:
                    await asyncio.to_thread(self._archive_entries, store, src, {entry.epoch})
                    rerouted += 1
                    logger.info(
                        "Snooze: rerouted entry (type=%s epoch=%d) %s → %s",
                        entry.entry_type,
                        entry.epoch,
                        src,
                        dst,
                    )
                    await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning("Snooze: reroute failed (epoch=%d): %s", entry.epoch, e)

        # ── Pass 2: LLM review for medium-confidence candidates ──────────
        if medium_conf and not did_llm_already and not self._is_cancelled():
            can_llm = self._llm_available() and bool(settings.background_model or settings.llm_model)
            if can_llm:
                catalog = _build_file_catalog(store)

                entry_lines = []
                for item in medium_conf[:10]:  # cap per cycle
                    e = item["entry"]
                    entry_lines.append(
                        f"epoch={e.epoch} | current_file={item['src_file']} | "
                        f"suggested_target={item['target_file']} | "
                        f"type={e.entry_type} | tags={','.join(e.tags[:6])} | "
                        f"content: {e.content[:250]}"
                    )

                prompt = self._REROUTE_PROMPT.format(
                    file_catalog=catalog,
                    entry_list="\n\n".join(entry_lines),
                )

                try:
                    from core.llm.client import get_llm_client

                    client = get_llm_client()
                    model = settings.background_model or settings.llm_model
                    response = await client.chat(
                        messages=[
                            {"role": "system", "content": "You are a memory file auditor."},
                            {"role": "user", "content": prompt},
                        ],
                        model=model,
                        max_tokens=1500,
                    )
                    used_llm = True

                    text = response.content.strip()
                    if text.startswith("```"):
                        lines = text.split("\n")
                        text = "\n".join(lines[1:])
                        if text.endswith("```"):
                            text = text[:-3]
                        text = text.strip()

                    decisions = _json.loads(text)
                    if isinstance(decisions, dict):
                        decisions = [decisions]

                    # Build lookups from candidate list
                    epoch_to_src: dict[int, str] = {item["entry"].epoch: item["src_file"] for item in medium_conf}
                    _known = await asyncio.to_thread(store.list_files)
                    known_files: set[str] = {f.name for f in _known}

                    # Count how many entries the LLM wants to send to each proposed new file.
                    # A new file is only justified if ≥2 entries share it (cluster threshold).
                    # Single-entry targets that aren't known files get downgraded to "keep".
                    new_file_counts: dict[str, int] = {}
                    for dec in decisions:
                        if not isinstance(dec, dict):
                            continue
                        if dec.get("action", "keep").lower() != "move":
                            continue
                        target = dec.get("target_file", "")
                        if target and target not in known_files:
                            new_file_counts[target] = new_file_counts.get(target, 0) + 1

                    for dec in decisions:
                        if not isinstance(dec, dict):
                            continue
                        if dec.get("action", "keep").lower() != "move":
                            continue
                        try:
                            epoch = int(dec["epoch"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        target = dec.get("target_file", "")
                        src = epoch_to_src.get(epoch)
                        if not src or not target or src == target:
                            continue
                        # New file: only allow if it's a genuine cluster (≥2 entries)
                        if target not in known_files:
                            if new_file_counts.get(target, 0) < 2:
                                logger.debug(
                                    "Snooze: reroute rejected single-entry new file %r "
                                    "(epoch=%d) — no cluster justification",
                                    target,
                                    epoch,
                                )
                                continue
                            logger.info(
                                "Snooze: reroute creating new file %r " "(%d entries justify it)",
                                target,
                                new_file_counts[target],
                            )

                        if self._is_cancelled():
                            break
                        try:
                            moved = store.move_entries(src, target, [epoch])
                            if moved:
                                await asyncio.to_thread(self._archive_entries, store, src, {epoch})
                                rerouted += 1
                                logger.info(
                                    "Snooze: LLM rerouted epoch=%d %s → %s (%s)",
                                    epoch,
                                    src,
                                    target,
                                    dec.get("reason", ""),
                                )
                                await asyncio.sleep(0.05)
                        except Exception as e:
                            logger.warning("Snooze: LLM reroute failed epoch=%d: %s", epoch, e)

                except Exception as e:
                    logger.warning("Snooze: reroute LLM call failed: %s", e)

        if rerouted:
            self._stats.setdefault("entries_rerouted", 0)
            self._stats["entries_rerouted"] += rerouted
            logger.info("Snooze: rerouted %d misplaced entries", rerouted)

        db.set_snooze_state("last_reroute_scan", datetime.now(timezone.utc).isoformat())
        return used_llm

    # ------------------------------------------------------------------
    # Activity 4: Tag enrichment
    # ------------------------------------------------------------------

    async def _enrich_tags(self) -> None:
        """Add heuristic tags to sparsely-tagged entries."""
        from core.memory.format import parse_entries_from_markdown
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if not store:
            return

        enriched = 0
        one_hour_ago = int(time.time()) - 3600

        _mem_files = await asyncio.to_thread(store.list_files)
        for mem_file in _mem_files:
            if self._is_cancelled() or enriched >= 10:
                break

            md_content = await asyncio.to_thread(store.read_file, mem_file.name)
            if not md_content:
                continue

            entries = parse_entries_from_markdown(mem_file.name, md_content)
            for entry in entries:
                if self._is_cancelled() or enriched >= 10:
                    break
                if len(entry.tags) >= 3 or entry.epoch > one_hour_ago:
                    continue

                new_tags = self._extract_tags(entry.content, entry.tags)
                if not new_tags:
                    continue

                # Update FTS5 index with enriched tags
                all_tags = list(set(entry.tags + new_tags))[:10]
                conn = store._connect()
                try:
                    # FTS5 doesn't support UPDATE — delete and reinsert
                    conn.execute(
                        "DELETE FROM memory_fts WHERE file_name = ? AND epoch = ?",
                        (entry.file_name, str(entry.epoch)),
                    )
                    conn.execute(
                        "INSERT INTO memory_fts "
                        "(file_name, content, tags, entry_type, weight, epoch, source, updated) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            entry.file_name,
                            entry.content,
                            ",".join(all_tags),
                            entry.entry_type,
                            entry.weight,
                            str(entry.epoch),
                            entry.source,
                            str(entry.updated),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                # Update markdown file tags
                self._update_tags_in_markdown(store, entry.file_name, entry.epoch, all_tags)
                enriched += 1

        if enriched:
            self._stats["entries_enriched"] += enriched
            logger.info("Snooze: enriched tags on %d entries", enriched)

    def _extract_tags(self, content: str, existing_tags: list[str]) -> list[str]:
        """Heuristic tag extraction from content."""
        existing_set = set(t.lower() for t in existing_tags)
        new_tags = []

        # Technical terms: words with underscores, dots, or camelCase
        tech_terms = re.findall(r"\b([a-z]+_[a-z_]+|[a-z]+\.[a-z.]+|[a-z]+[A-Z][a-zA-Z]+)\b", content)
        for term in tech_terms:
            t = term.lower()
            if t not in existing_set and len(t) > 3:
                new_tags.append(t)
                existing_set.add(t)

        # Capitalized proper nouns (2+ chars, not at sentence start)
        # Simple heuristic: words that are capitalized and not common English
        proper_nouns = re.findall(r"(?<=\s)[A-Z][a-z]{2,}", content)
        common = {
            "the",
            "this",
            "that",
            "when",
            "where",
            "what",
            "how",
            "which",
            "there",
            "here",
            "with",
            "from",
            "into",
            "upon",
            "about",
            "after",
            "before",
            "during",
            "between",
            "through",
            "error",
            "note",
            "found",
        }
        for noun in proper_nouns:
            t = noun.lower()
            if t not in existing_set and t not in common and len(t) > 2:
                new_tags.append(t)
                existing_set.add(t)

        return new_tags[:5]  # max 5 new tags per entry

    def _update_tags_in_markdown(self, store, file_name: str, epoch: int, tags: list[str]) -> None:
        """Update or add @tags comment in markdown for an entry."""
        import fcntl

        md_path = store._dir / f"{file_name}.md"
        if not md_path.exists():
            return

        with store._lock:
            content = md_path.read_text()
            epoch_marker = f"<!-- @epoch: {epoch} -->"
            if epoch_marker not in content:
                return

            new_tags_line = f"<!-- @tags: {','.join(tags)} -->"

            # Check if tags line already exists for this entry
            # Find the section for this epoch
            parts = content.split(epoch_marker)
            if len(parts) < 2:
                return

            after = parts[1]
            # Replace existing tags line or insert after epoch
            existing_tags = re.search(r"<!-- @tags:.*?-->", after[:200])
            if existing_tags:
                after = after[: existing_tags.start()] + new_tags_line + after[existing_tags.end() :]
            else:
                after = "\n" + new_tags_line + after

            content = parts[0] + epoch_marker + after
            with open(md_path, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(content)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # Activity 5: Index reconciliation
    # ------------------------------------------------------------------

    async def _reconcile_index(self) -> None:
        """Check and fix FTS5 index drift."""
        from core.memory.store import get_memory_store
        from db import models as db

        store = get_memory_store()
        if not store:
            return

        # Check if reconciliation is due (6 hours)
        last = db.get_snooze_state("last_index_reconcile")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < 21600:  # 6 hours
                    return
            except ValueError:
                pass

        if self._is_cancelled():
            return

        health = await asyncio.to_thread(store.health_check, fix=True)
        db.set_snooze_state("last_index_reconcile", datetime.now(timezone.utc).isoformat())

        if health.get("action") == "reindexed":
            logger.info("Snooze: index reconciliation triggered reindex (%s)", health)
        else:
            logger.debug("Snooze: index in sync (%s)", health)

    # ------------------------------------------------------------------
    # Activity 6: Memory file splitting
    # ------------------------------------------------------------------

    async def _split_file(self) -> bool:
        """Split bloated memory files using LLM-assisted grouping.

        Returns True if an LLM call was made (so caller can set did_maintenance_llm).
        Entries are moved (not duplicated): source entries are archived after the
        corresponding target entries are written.
        """
        from core.memory.format import parse_entries_from_markdown
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if not store:
            return False

        # Find the most bloated file (>= 80 active entries)
        target = None
        _all_files = await asyncio.to_thread(store.list_files)
        for f in sorted(_all_files, key=lambda x: x.entry_count, reverse=True):
            if f.entry_count >= 80:
                target = f
                break

        if not target:
            return False

        if self._is_cancelled():
            return False

        logger.info("Snooze: splitting bloated file %s (%d entries)", target.name, target.entry_count)

        md_content = await asyncio.to_thread(store.read_file, target.name)
        if not md_content:
            return False

        entries = parse_entries_from_markdown(target.name, md_content)
        if len(entries) < 80:
            return False

        # Cap at 150 entries per cycle to keep the LLM prompt manageable;
        # subsequent cycles will continue shrinking the file.
        sample = entries[:150]
        entry_summaries = [f"{i}: [{e.entry_type}] {e.content[:150]}" for i, e in enumerate(sample)]

        _existing = await asyncio.to_thread(store.list_files)
        existing_files = [f.name for f in _existing]

        from core.llm.client import get_llm_client

        client = get_llm_client()
        model = settings.background_model or settings.llm_model

        prompt = (
            f"These {len(sample)} memory entries are currently all stored in '{target.name}'. "
            f"Re-group them into 2-4 more specific files.\n\n"
            f"EXISTING FILES — prefer routing to these where they fit; "
            f"only propose a NEW name if multiple entries share a coherent topic not covered by any existing file:\n"
            f"{', '.join(existing_files)}\n\n"
            f"Entries:\n" + "\n".join(entry_summaries) + "\n\n"
            f"Rules:\n"
            f"- Every entry must appear in exactly one group.\n"
            f"- New file names must use dot-separated lowercase (e.g., pernix.workers, pernix.automation).\n"
            f"- A small residual group may remain in '{target.name}'.\n\n"
            f'Output JSON only: {{"groups": [{{"file": "name.here", "entries": [0, 1, 5]}}]}} /no_think'
        )

        try:
            response = await client.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                max_tokens=2000,
            )
        except Exception as e:
            logger.warning("Snooze: file split LLM call failed: %s", e)
            return True  # LLM was attempted; count against maintenance budget

        if self._is_cancelled():
            return True

        # Parse groupings
        import json

        try:
            text = response.content.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text.strip())
            groups = data.get("groups", [])
        except (json.JSONDecodeError, KeyError, IndexError):
            logger.warning("Snooze: could not parse file split response")
            return True

        # Build per-target-file epoch lists; deduplicate so each epoch goes to at most one file.
        seen_epochs: set[int] = set()
        epochs_by_file: dict[str, list[int]] = {}
        for group in groups:
            file_name = group.get("file", "")
            indices = group.get("entries", [])
            # Sanitize LLM-generated filename (defense-in-depth)
            file_name = file_name.replace("/", ".").replace("\\", ".").replace("..", ".")
            if not file_name or not indices or file_name == target.name:
                continue
            unique_epochs = []
            for idx in indices:
                if 0 <= idx < len(sample):
                    ep = sample[idx].epoch
                    if ep not in seen_epochs:
                        seen_epochs.add(ep)
                        unique_epochs.append(ep)
            if unique_epochs:
                epochs_by_file[file_name] = unique_epochs

        if not epochs_by_file:
            return True

        # Move entries: write to target files, then archive in source.
        # move_entries handles FTS indexing and hit-count migration.
        all_moved_epochs: set[int] = set()
        moved = 0
        for file_name, epoch_list in epochs_by_file.items():
            if self._is_cancelled():
                break
            count = store.move_entries(target.name, file_name, epoch_list)
            if count > 0:
                all_moved_epochs.update(epoch_list)
                moved += count
                logger.debug("Snooze: split %d entries → %s", count, file_name)

        if moved and all_moved_epochs:
            await asyncio.to_thread(self._archive_entries, store, target.name, all_moved_epochs)
            self._stats.setdefault("entries_split", 0)
            self._stats["entries_split"] += moved
            logger.info(
                "Snooze: split %d entries from %s into %d file(s)",
                moved,
                target.name,
                len(epochs_by_file),
            )

        return True

    # ------------------------------------------------------------------
    # Activity 7: Cron cleanup (no LLM)
    # ------------------------------------------------------------------

    async def _cleanup_cron(self, bus=None) -> None:
        """Prune old cron run records and cron-created sessions."""
        from db import models as _db

        # Check interval: once per 6 hours
        try:
            last = _db.get_snooze_state("last_cron_cleanup")
            if last:
                elapsed = time.time() - float(last)
                if elapsed < 6 * 3600:
                    return
        except Exception:
            pass

        if bus:
            bus.emit(
                {"type": "snooze.activity", "activity": "cron_cleanup", "detail": "Pruning old cron runs and sessions"}
            )

        runs_pruned = _db.prune_cron_runs(max_age_days=30, keep_per_job=100)
        sessions_pruned = _db.prune_cron_sessions(max_age_days=7)
        # State-log retention: keep 500 most recent rows per session regardless
        # of age (so the last turn of every session is always inspectable),
        # and drop anything older than 30 days beyond that floor.
        state_log_pruned = _db.prune_state_log(max_age_days=30, keep_per_session=500)

        if runs_pruned or sessions_pruned or state_log_pruned:
            logger.info(
                "Snooze cron cleanup: %d runs pruned, %d sessions pruned, %d state_log rows pruned",
                runs_pruned,
                sessions_pruned,
                state_log_pruned,
            )
            self._stats.setdefault("cron_runs_pruned", 0)
            self._stats["cron_runs_pruned"] += runs_pruned
            self._stats.setdefault("cron_sessions_pruned", 0)
            self._stats["cron_sessions_pruned"] += sessions_pruned
            self._stats.setdefault("state_log_rows_pruned", 0)
            self._stats["state_log_rows_pruned"] += state_log_pruned

        _db.set_snooze_state("last_cron_cleanup", str(time.time()))

    # ------------------------------------------------------------------
    # Activity 8: Staleness pruning (LLM-gated)
    # ------------------------------------------------------------------

    _STALE_PRUNE_PROMPT = """You are a memory curator. These memory entries have low recall rates relative to their age cohort — they are retrieved less often than similar-aged entries.

For each entry, decide:
- KEEP: Still valuable despite low usage (foundational fact, rare but irreplaceable knowledge, identity/preference info)
- PRUNE: Safe to archive (transient context that served its purpose, outdated project state, superseded information)

Err on the side of KEEP when uncertain. Only PRUNE entries that are clearly stale or redundant.

Output a JSON array: [{"epoch": <number>, "verdict": "keep|prune", "reason": "brief reason"}]
Output valid JSON only. No markdown fences. /no_think"""

    async def _prune_stale_entries(self) -> None:
        """Archive low-recall entries using age-cohort analysis + LLM gatekeeper."""
        from core.memory.store import get_memory_store
        from db import models as db

        store = get_memory_store()
        if not store:
            return

        # Check interval: once per 7 days
        interval_days = settings.snooze_dedup_interval_days  # reuse same interval
        last = db.get_snooze_state("last_stale_prune")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < interval_days * 86400:
                    return
            except ValueError:
                pass

        if self._is_cancelled():
            return

        # Step 1: Query all entries with their hit counts.
        # No LIMIT — scans the full FTS table; push off-loop to keep event
        # loop responsive as the store grows.
        def _fetch_all_entries():
            c = store._connect()
            try:
                return c.execute("""SELECT f.file_name, f.epoch, f.weight, f.content,
                              COALESCE(h.hit_count, 0) as hit_count
                       FROM memory_fts f
                       LEFT JOIN memory_hits h
                         ON f.file_name = h.file_name AND f.epoch = h.epoch
                       ORDER BY CAST(f.epoch AS INTEGER) ASC""").fetchall()
            finally:
                c.close()

        rows = await asyncio.to_thread(_fetch_all_entries)

        if not rows or len(rows) < 20:
            # Not enough data for meaningful cohort analysis
            db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())
            return

        if self._is_cancelled():
            return

        # Step 2: Bucket entries by age cohort and compute stats
        now = int(time.time())
        cohorts: dict[str, list[dict]] = {
            "30d": [],
            "60d": [],
            "90d": [],
            "180d": [],
            "360d": [],
        }

        for row in rows:
            try:
                age_days = (now - int(row["epoch"])) / 86400
            except (ValueError, TypeError):
                continue
            entry = {
                "file_name": row["file_name"],
                "epoch": row["epoch"],
                "weight": row["weight"],
                "content": row["content"],
                "hit_count": row["hit_count"],
                "age_days": age_days,
            }
            if age_days >= 360:
                cohorts["360d"].append(entry)
            elif age_days >= 180:
                cohorts["180d"].append(entry)
            elif age_days >= 90:
                cohorts["90d"].append(entry)
            elif age_days >= 60:
                cohorts["60d"].append(entry)
            elif age_days >= 30:
                cohorts["30d"].append(entry)
            # < 30 days: never prune

        # Step 3: Find candidates below cohort average
        candidates = []
        for cohort_name, entries in cohorts.items():
            if len(entries) < 3:
                continue  # need enough data for meaningful average

            hit_counts = [e["hit_count"] for e in entries]
            avg_hits = sum(hit_counts) / len(hit_counts)

            for entry in entries:
                # Skip high-weight entries (explicitly marked important)
                if entry["weight"] == "high":
                    continue
                # Below average OR zero hits for 60d+ entries
                is_below_avg = entry["hit_count"] < avg_hits
                is_zero_old = entry["hit_count"] == 0 and entry["age_days"] >= 60
                if is_below_avg or is_zero_old:
                    entry["cohort"] = cohort_name
                    entry["cohort_avg"] = avg_hits
                    candidates.append(entry)

        if not candidates:
            db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())
            return

        # Cap at 10 per cycle
        candidates = candidates[:10]

        if self._is_cancelled():
            return

        # Step 4: LLM gatekeeper
        entry_descriptions = []
        for c in candidates:
            entry_descriptions.append(
                f"epoch={c['epoch']} | file={c['file_name']} | "
                f"hits={c['hit_count']} (cohort avg={c['cohort_avg']:.1f}, bucket={c['cohort']}) | "
                f"content: {c['content'][:200]}"
            )

        from core.llm.client import get_llm_client

        client = get_llm_client()
        model = settings.background_model or settings.llm_model

        try:
            response = await client.chat(
                messages=[
                    {"role": "system", "content": self._STALE_PRUNE_PROMPT},
                    {"role": "user", "content": "\n\n".join(entry_descriptions)},
                ],
                model=model,
                max_tokens=1500,
            )
        except Exception as e:
            logger.warning("Snooze: stale prune LLM call failed: %s", e)
            db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())
            return

        if self._is_cancelled():
            return

        # Step 5: Parse verdicts and execute pruning
        import json as _json

        text = response.content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            verdicts = _json.loads(text)
            if isinstance(verdicts, dict):
                verdicts = [verdicts]
        except _json.JSONDecodeError:
            logger.warning("Snooze: could not parse stale prune response")
            db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())
            return

        # Build epoch-to-file map from candidates
        candidate_map = {str(c["epoch"]): c for c in candidates}

        pruned_by_file: dict[str, set[int]] = {}
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            epoch_str = str(v.get("epoch", ""))
            verdict = v.get("verdict", "keep").lower()
            if verdict != "prune" or epoch_str not in candidate_map:
                continue
            c = candidate_map[epoch_str]
            file_name = c["file_name"]
            pruned_by_file.setdefault(file_name, set())
            pruned_by_file[file_name].add(int(epoch_str))
            logger.debug(
                "Snooze: pruning stale entry epoch=%s file=%s reason=%s", epoch_str, file_name, v.get("reason", "")
            )

        total_pruned = 0
        for file_name, epochs in pruned_by_file.items():
            if self._is_cancelled():
                break
            await asyncio.to_thread(self._archive_entries, store, file_name, epochs)
            total_pruned += len(epochs)

        if total_pruned:
            self._stats.setdefault("entries_pruned", 0)
            self._stats["entries_pruned"] += total_pruned
            logger.info("Snooze: pruned %d stale entries across %d files", total_pruned, len(pruned_by_file))

        db.set_snooze_state("last_stale_prune", datetime.now(timezone.utc).isoformat())

    # ------------------------------------------------------------------
    # Activity 9: Skill cooccurrence update (no LLM)
    # ------------------------------------------------------------------

    async def _update_skill_cooccurrence(self) -> None:
        """Populate SKILL_COOCCURRENCE from skill-type memory entries and surface
        un-promoted skill clusters.

        Groups skill-type entries by shared tags, cross-references against the
        skill registry, and builds cooccurrence links.
        """
        from core.memory.store import get_memory_store
        from core.skills.registry import get_skill_registry
        from db import models as db

        store = get_memory_store()
        if not store:
            return

        # Check if we have new skill entries since last run
        last = db.get_snooze_state("last_skill_cooccurrence")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < 3600:  # 1 hour
                    return
            except ValueError:
                pass

        if self._is_cancelled():
            return

        # Query all skill-type entries from FTS5
        conn = store._connect()
        try:
            rows = conn.execute(
                "SELECT file_name, content, tags, epoch FROM memory_fts WHERE entry_type = 'skill'"
            ).fetchall()
        finally:
            conn.close()

        if not rows or len(rows) < 2:
            db.set_snooze_state("last_skill_cooccurrence", datetime.now(timezone.utc).isoformat())
            return

        # Parse tags and build tag-to-entries index
        tag_to_entries: dict[str, list[str]] = {}  # tag -> list of file_names
        entry_tags: dict[str, set[str]] = {}  # epoch -> set of tags

        for row in rows:
            epoch = row["epoch"]
            tags_str = row["tags"] or ""
            tags = {t.strip().lower() for t in tags_str.split(",") if t.strip() and len(t.strip()) > 2}
            # Exclude date-like tags
            tags = {t for t in tags if not re.match(r"^\d{4}-\d{2}-\d{2}$", t)}
            entry_tags[epoch] = tags
            for tag in tags:
                tag_to_entries.setdefault(tag, [])
                tag_to_entries[tag].append(row["file_name"])

        if self._is_cancelled():
            return

        # Cross-reference with registered, enabled skills only.
        # Disabled skills shouldn't accumulate cooccurrence training data — when
        # re-enabled they'd come back with stale boost links from a period the
        # user had explicitly turned them off.
        registry = get_skill_registry()
        registered_skills = registry.enabled_skills()
        skill_tag_map: dict[str, set[str]] = {}  # skill_name -> tags
        for skill in registered_skills:
            skill_tag_map[skill.name] = {t.lower() for t in skill.tags}

        # Build cooccurrence: if a registered skill shares 2+ tags with another
        # registered skill via the memory skill entries, they are related
        cooccurrence: dict[str, list[str]] = {}
        skill_names = list(skill_tag_map.keys())

        for i, s1 in enumerate(skill_names):
            for s2 in skill_names[i + 1 :]:
                # Find tags shared through skill-type memory entries
                s1_tags = skill_tag_map[s1]
                s2_tags = skill_tag_map[s2]
                shared = s1_tags & s2_tags
                if len(shared) >= 2:
                    cooccurrence.setdefault(s1, [])
                    cooccurrence.setdefault(s2, [])
                    if s2 not in cooccurrence[s1]:
                        cooccurrence[s1].append(s2)
                    if s1 not in cooccurrence[s2]:
                        cooccurrence[s2].append(s1)

        # Also: find memory skill entries whose tags overlap with registered skills
        for skill_name, skill_tags in skill_tag_map.items():
            for tag in skill_tags:
                if tag in tag_to_entries:
                    # Memory entries share this tag with the skill
                    related_files = set(tag_to_entries[tag])
                    for other_skill, other_tags in skill_tag_map.items():
                        if other_skill != skill_name and tag in other_tags:
                            cooccurrence.setdefault(skill_name, [])
                            if other_skill not in cooccurrence[skill_name]:
                                cooccurrence[skill_name].append(other_skill)

        if cooccurrence:
            registry.update_cooccurrence(cooccurrence)
            logger.info("Snooze: updated skill cooccurrence with %d links", sum(len(v) for v in cooccurrence.values()))

        # Surface un-promoted skill clusters: tag groups with 3+ skill entries but no registered skill
        tag_counts: dict[str, int] = {}
        for tag, entries in tag_to_entries.items():
            tag_counts[tag] = len(set(entries))  # unique file_names

        all_skill_tags = set()
        for tags in skill_tag_map.values():
            all_skill_tags.update(tags)

        orphan_clusters = []
        for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
            if count >= 3 and tag not in all_skill_tags:
                orphan_clusters.append((tag, count))

        if orphan_clusters:
            cluster_summary = ", ".join(f"{tag}({count})" for tag, count in orphan_clusters[:5])
            logger.info("Snooze: un-promoted skill clusters detected: %s", cluster_summary)

        db.set_snooze_state("last_skill_cooccurrence", datetime.now(timezone.utc).isoformat())

    # ------------------------------------------------------------------
    # Activity 10: Synthesize post-mortems into tool/skill performance counters (no LLM)
    # ------------------------------------------------------------------

    async def _cleanup_post_mortems(self) -> None:
        """Delete synthesized post-mortems older than the retention window.

        Only sweeps rows already processed by synthesis — never touches the
        unsynthesized backlog, so a backlogged run won't lose data. Cheap
        no-op once caught up.
        """
        from db import models as db

        retention_days = max(int(settings.post_mortem_retention_days or 0), 1)
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        try:
            deleted = db.delete_old_post_mortems(cutoff_iso)
            if deleted:
                logger.info(
                    "Snooze post-mortem cleanup: deleted %d rows older than %d days",
                    deleted,
                    retention_days,
                )
                self._stats.setdefault("post_mortems_pruned", 0)
                self._stats["post_mortems_pruned"] += deleted
        except Exception as e:
            logger.warning("Snooze post-mortem cleanup failed: %s", e)

    async def _cleanup_workflow_runs(self) -> None:
        """Delete old workflow run directories and DB rows beyond retention limits.

        Keeps the 10 most recent completed runs per workflow. Also deletes any
        run older than 30 days regardless of count. Running runs are never touched.
        """
        import shutil
        from pathlib import Path

        from db import models as db

        _MAX_RUNS_PER_WORKFLOW = 10
        _MAX_AGE_DAYS = 30

        try:
            runs = db.list_workflow_runs(limit=1000)
        except Exception as e:
            logger.warning("Snooze workflow cleanup: could not list runs: %s", e)
            return

        from datetime import timedelta, timezone

        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=_MAX_AGE_DAYS)).isoformat()

        # Group completed runs by workflow name (exclude running ones)
        by_workflow: dict[str, list[dict]] = {}
        for run in runs:
            if run.get("status") == "running":
                continue
            wname = run["workflow_name"]
            by_workflow.setdefault(wname, []).append(run)

        to_delete: list[dict] = []
        for wname, wf_runs in by_workflow.items():
            # Sort newest first. Runs with missing started_at sort to the front
            # (empty string < any ISO timestamp) so they appear as "oldest" —
            # exclude them from the keep window to avoid deleting recent runs.
            wf_runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
            for i, run in enumerate(wf_runs):
                started = run.get("started_at") or ""
                if i >= _MAX_RUNS_PER_WORKFLOW or (started and started < cutoff_iso):
                    to_delete.append(run)

        if not to_delete:
            return

        workspace_dir = Path(settings.workspace_dir)
        deleted = 0
        for run in to_delete:
            run_id = run["run_id"]
            run_dir = workspace_dir / run["run_dir"]
            try:
                # Archive pending proposals before deleting
                db.archive_proposals_for_run(run_id)
                # Remove directory — rmtree is blocking filesystem recursion;
                # offload so the event loop stays responsive across many deletions.
                if run_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, run_dir)
                # Remove DB row
                db.delete_workflow_run(run_id)
                deleted += 1
            except Exception as e:
                logger.warning("Snooze workflow cleanup: error deleting run %s: %s", run_id, e)

        if deleted:
            logger.info("Snooze workflow cleanup: deleted %d old run(s)", deleted)

    async def _synthesize_signals(self) -> None:
        """Run one batch of post-mortem → tool/skill performance synthesis.

        Pure SQL + attribution rules; no LLM calls. Runs every cycle; the
        `synthesized_at` watermark makes repeat cycles cheap no-ops once
        caught up. Bounded batch size keeps a single cycle's work finite
        even if a backlog accumulates.
        """
        from core import synthesis

        try:
            stats = await asyncio.to_thread(synthesis.run, 500)
            if stats.processed:
                logger.info(
                    "Snooze synthesis: %d post-mortems → %d signal updates",
                    stats.processed,
                    stats.attributions,
                )
        except Exception as e:
            logger.warning("Snooze synthesis failed: %s", e)


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_runner: SnoozeRunner | None = None


def get_snooze() -> SnoozeRunner:
    global _runner
    if _runner is None:
        _runner = SnoozeRunner()
    return _runner
