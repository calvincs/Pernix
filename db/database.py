"""Pernix — SQLite database initialization and versioned migrations."""

import logging
import sqlite3
import threading
from pathlib import Path

from config import settings

logger = logging.getLogger("pernix.db")

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

# Per-thread connection cache. Every db.models helper used to open a fresh
# connection (+ replay 5 PRAGMAs) per call — hundreds of times per turn.
# Connections are reused per (thread, path); `with conn:` blocks commit but
# never close, so reuse is transparent. Verified: no code path nests
# connection contexts to the same DB in one thread, so a shared connection
# cannot commit another block's in-flight transaction. That invariant is
# enforced by tests/test_db_invariants.py, not just by this comment.
_conn_local = threading.local()
_CONN_CACHE_MAX = 4  # sessions + memory + headroom for test tmp paths


class _TrackedConnection(sqlite3.Connection):
    """Connection that knows whether a `with` block is currently holding it.

    Cache eviction used to close *every* cached connection unconditionally,
    including one an outer stack frame was mid-`with` on — the caller's next
    statement would then raise ProgrammingError and its in-flight transaction
    would be lost. That was safe only by arithmetic coincidence (prod uses two
    paths, the cap is four). Tracking checkouts makes it safe by construction:
    eviction skips anything currently held.
    """

    # Class-level default so the attribute exists before the first __enter__.
    _checkouts = 0

    def __enter__(self):
        self._checkouts += 1
        return super().__enter__()

    def __exit__(self, *exc_info):
        try:
            return super().__exit__(*exc_info)
        except Exception:
            # A COMMIT that fails (busy timeout under a long writer) leaves
            # the transaction OPEN on a connection this thread will reuse:
            # the next `with conn:` block inherits it, and that block's own
            # commit or rollback then decides the fate of these writes too.
            # Roll back here so each block's outcome stays its own.
            try:
                super().rollback()
            except Exception:
                pass
            raise
        finally:
            self._checkouts -= 1


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    """Return this thread's cached SQLite connection for the path (opening
    and configuring it on first use)."""
    path = db_path or settings.db_path
    cache: dict = getattr(_conn_local, "conns", None) or {}
    if not hasattr(_conn_local, "conns"):
        _conn_local.conns = cache
    conn = cache.get(path)
    if conn is not None:
        try:
            conn.total_changes  # cheap liveness probe — raises if closed
            return conn
        except sqlite3.ProgrammingError:
            del cache[path]
    if len(cache) >= _CONN_CACHE_MAX:
        # Evict idle entries only (tests rotate tmp DB paths; prod uses 2).
        # A connection checked out by an outer frame is left in place — the
        # cap is a soft target, and briefly exceeding it costs one file handle
        # whereas closing a live connection costs a transaction.
        for stale_path, stale in list(cache.items()):
            if getattr(stale, "_checkouts", 0) > 0:
                continue
            try:
                stale.close()
            except Exception:
                pass
            del cache[stale_path]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, factory=_TrackedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA wal_autocheckpoint=1000")  # checkpoint every ~4MB of writes
    cache[path] = conn
    return conn


def connect_sessions() -> sqlite3.Connection:
    """Connect to the sessions database."""
    return _connect(settings.db_path)


def connect_memory() -> sqlite3.Connection:
    """Connect to the memory FTS5 database (separate from sessions)."""
    db_path = str(Path(settings.memory_dir) / "_index.db")
    return _connect(db_path)


# ---------------------------------------------------------------------------
# Directory setup
# ---------------------------------------------------------------------------


def ensure_dirs() -> None:
    """Create all required data directories."""
    for d in [
        "data",
        settings.workspace_dir,
        settings.memory_dir,
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Sessions DB schema (version 1 — initial)
# ---------------------------------------------------------------------------

_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New session',
    system_prompt TEXT NOT NULL DEFAULT '',
    session_type TEXT DEFAULT 'normal',
    parent_session_id TEXT,
    -- Retired: the pre-v2 5-state session enum. The state machine lives in
    -- state_v2 (added by migration v16) and no longer writes this column;
    -- see the v16 entry in MIGRATIONS. Kept rather than dropped because
    -- SQLite would need a full table rebuild and the column is harmless —
    -- its only remaining writers are create_session (seeds 'idle') and the
    -- RLM view-session busy marker in core/extensions/rlm.
    state TEXT DEFAULT 'idle',
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_parent
    ON sessions(parent_session_id) WHERE parent_session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT,
    tool_calls TEXT,
    char_count INTEGER NOT NULL DEFAULT 0,
    token_count INTEGER,
    partial INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency
    ON messages(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'text/html',
    version INTEGER DEFAULT 1,
    parent_id TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_estimate REAL,
    source TEXT NOT NULL DEFAULT 'provider',
    provider TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_token_usage_session ON token_usage(session_id);

CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    session_title TEXT NOT NULL DEFAULT '',
    session_type TEXT NOT NULL DEFAULT 'normal',
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT 'normal',
    question_type TEXT NOT NULL DEFAULT 'question',
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_questions_session
    ON questions(session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    session_id TEXT DEFAULT '',
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_session_messages_recipient
    ON session_messages(recipient_id, read_at);

CREATE TABLE IF NOT EXISTS cron_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    session_id TEXT,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_cron_runs_job
    ON cron_runs(job_name, started_at DESC);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Memory DB schema
# ---------------------------------------------------------------------------

_MEMORY_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    file_name,
    content,
    tags,
    entry_type,
    weight,
    epoch UNINDEXED,
    source UNINDEXED,
    updated UNINDEXED,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS memory_files (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    keywords TEXT NOT NULL,
    entry_count INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_hits (
    file_name TEXT NOT NULL,
    epoch TEXT NOT NULL,
    hit_count INTEGER DEFAULT 0,
    last_hit_at INTEGER DEFAULT 0,
    PRIMARY KEY (file_name, epoch)
);

CREATE TABLE IF NOT EXISTS consolidation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_file TEXT NOT NULL,
    source_files TEXT NOT NULL,
    strategy TEXT NOT NULL,
    entries_kept INTEGER NOT NULL,
    entries_archived INTEGER NOT NULL,
    reason TEXT NOT NULL,
    executed_at INTEGER NOT NULL
);

-- Semantic-retrieval sidecar (adaptation plan 1f). Rebuildable like FTS:
-- markdown stays the source of truth (I3). Keyed on the composite
-- (file_name, epoch) — epochs are unique only per file. content_hash marks
-- staleness; model guards against mixed embedding spaces. reindex() prunes
-- orphans but never re-embeds (embedding is snooze work, off the sync
-- startup path).
CREATE TABLE IF NOT EXISTS vectors (
    file_name TEXT NOT NULL,
    epoch TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    vec BLOB NOT NULL,
    updated_at INTEGER DEFAULT 0,
    PRIMARY KEY (file_name, epoch)
);

CREATE TABLE IF NOT EXISTS vectors_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Background jobs (job_start/job_status/job_tail/job_kill). Rows outlive the
-- process: the exit code is written by the job's wrapper shell to a sidecar
-- file, and job_status lazily reconciles state from pid + sidecar.
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    command TEXT NOT NULL,
    pid INTEGER,
    state TEXT NOT NULL DEFAULT 'running',
    exit_code INTEGER,
    created_at TEXT NOT NULL,
    deadline_s INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT,
    log_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id, created_at);
"""

# ---------------------------------------------------------------------------
# Versioned migrations
# ---------------------------------------------------------------------------

# Each migration is (version, description, sql_statements).
# sql_statements is a list of individual SQL strings.
# Migrations run in order; schema_meta tracks the current version.
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    # Version 1 is the initial schema created by _SESSIONS_SCHEMA.
    (
        2,
        "add snooze support",
        [
            "ALTER TABLE sessions ADD COLUMN snooze_reviewed_at TEXT",
            """CREATE TABLE IF NOT EXISTS snooze_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        ],
    ),
    (
        3,
        "add messages FTS5 for cross-session search",
        [
            """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            session_id UNINDEXED,
            role UNINDEXED,
            content,
            tokenize='porter unicode61'
        )""",
            # Backfill existing messages (only user/assistant/tool with content)
            """INSERT INTO messages_fts (rowid, session_id, role, content)
           SELECT id, session_id, role, content FROM messages
           WHERE role IN ('user', 'assistant', 'tool') AND length(content) > 10""",
        ],
    ),
    (
        4,
        "merge orchestrator into normal session type",
        [
            "UPDATE sessions SET session_type = 'normal' WHERE session_type = 'orchestrator'",
            "UPDATE questions SET session_type = 'normal' WHERE session_type = 'orchestrator'",
        ],
    ),
    (
        5,
        "add metadata column to messages, add session_role index",
        [
            "ALTER TABLE messages ADD COLUMN metadata TEXT",
            # Migrate existing compaction metadata from tool_calls to metadata
            """UPDATE messages SET metadata = tool_calls, tool_calls = NULL
           WHERE role = 'compaction' AND tool_calls IS NOT NULL""",
            # Composite index for efficient session listing and FTS queries
            """CREATE INDEX IF NOT EXISTS idx_messages_session_role
           ON messages(session_id, role)""",
        ],
    ),
    (
        6,
        "add subtitle column, reset broken thinking-process titles",
        [
            "ALTER TABLE sessions ADD COLUMN subtitle TEXT DEFAULT ''",
            # Reset titles that contain thinking/reasoning dumps from thinking models
            "UPDATE sessions SET title = 'New session' WHERE title LIKE 'Thinking Process%'",
            "UPDATE sessions SET title = 'New session' WHERE title LIKE '<think>%'",
        ],
    ),
    (
        7,
        "add project and workspace_path to artifacts for unified workspace",
        [
            "ALTER TABLE artifacts ADD COLUMN project TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE artifacts ADD COLUMN workspace_path TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project)",
        ],
    ),
    (
        8,
        "add latency_ms to messages for tool execution tracking",
        [
            "ALTER TABLE messages ADD COLUMN latency_ms INTEGER DEFAULT NULL",
        ],
    ),
    (
        9,
        "add push_subscriptions for Web Push VAPID",
        [
            """CREATE TABLE IF NOT EXISTS push_subscriptions (
            id TEXT PRIMARY KEY,
            endpoint TEXT UNIQUE NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT
        )""",
        ],
    ),
    (
        10,
        "add post_mortems for reflect-as-compiler artifacts (Phase 2c)",
        [
            # One row per reflect invocation. Serves as the feedback artifact that
            # snooze consumes to synthesize curated scout_hints. Structured columns
            # for indexed querying; payload JSON for full detail.
            """CREATE TABLE IF NOT EXISTS post_mortems (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            verdict TEXT NOT NULL,
            failure_cause TEXT NOT NULL DEFAULT 'none',
            confidence REAL NOT NULL DEFAULT 0.0,
            reflect_model TEXT,
            reflect_latency_ms INTEGER,
            scout_viability TEXT,
            execution_mode TEXT,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )""",
            """CREATE INDEX IF NOT EXISTS idx_postmortems_session
           ON post_mortems(session_id, created_at DESC)""",
            """CREATE INDEX IF NOT EXISTS idx_postmortems_cause
           ON post_mortems(failure_cause, created_at DESC)""",
        ],
    ),
    (
        11,
        "add scout_signals for snooze-curated observations (Phase 3a)",
        [
            # Observations aggregated from post_mortems by snooze. Named "signals"
            # rather than "hints" because snooze observes outcomes — scout decides.
            # Natural PK (signal_type, subject): one row per (kind, name) pair.
            # Counters split 3-way to allow asymmetric weighting at read time:
            # failures can be weighted heavier than successes in strength().
            # user_approved nullable from the start to avoid a later migration:
            #   NULL = unset (default, auto-derived), 1 = user approved, 0 = tombstoned.
            """CREATE TABLE IF NOT EXISTS scout_signals (
            signal_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            reinforcements INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_reinforced_at TEXT NOT NULL,
            user_approved INTEGER DEFAULT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (signal_type, subject)
        )""",
            # Recency cutoff queries (top-N within N days).
            """CREATE INDEX IF NOT EXISTS idx_scout_signals_recency
           ON scout_signals(last_reinforced_at DESC)""",
        ],
    ),
    (
        12,
        "add post_mortems.synthesized_at watermark column (Phase 3b)",
        [
            # NULL = not yet processed by snooze synthesis; ISO timestamp = processed.
            # Synthesis queries `WHERE synthesized_at IS NULL` for exactly-once semantics
            # without relying on clock-based watermarks.
            "ALTER TABLE post_mortems ADD COLUMN synthesized_at TEXT DEFAULT NULL",
            """CREATE INDEX IF NOT EXISTS idx_postmortems_synth
           ON post_mortems(synthesized_at, created_at)""",
        ],
    ),
    (
        13,
        "add session_state_log for true state machine forensics (Phase 4)",
        [
            # One row per state transition. The in-memory session.events deque is
            # bounded (2000) and lost when a session is reaped; this table is
            # durable and queryable long after the fact. Written synchronously
            # inside the state-machine mutator under session.lock — a single
            # INSERT on WAL is sub-millisecond.
            #
            # turn_id: monotonic per session; increments on IDLE_READY→SCOUTING
            #          and AWAITING_USER→SCOUTING (in which case parent_turn_id
            #          points to the original).
            # retry_index: 0 for the initial attempt within a turn, 1..N for
            #              reflect/eval retries (FINALIZING→SCOUTING re-entries).
            # compaction_count: how many PROCESSING↔COMPACTING round-trips have
            #                   happened at the current (turn_id, retry_index).
            # reason: small controlled vocabulary so the log stays analyzable.
            """CREATE TABLE IF NOT EXISTS session_state_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_id INTEGER NOT NULL,
            parent_turn_id INTEGER,
            retry_index INTEGER NOT NULL DEFAULT 0,
            compaction_count INTEGER NOT NULL DEFAULT 0,
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            termination_reason TEXT,
            reflect_count INTEGER NOT NULL DEFAULT 0,
            eval_count INTEGER NOT NULL DEFAULT 0,
            timestamp_ms INTEGER NOT NULL,
            elapsed_ms INTEGER
        )""",
            """CREATE INDEX IF NOT EXISTS idx_state_log_session_time
           ON session_state_log(session_id, timestamp_ms)""",
            """CREATE INDEX IF NOT EXISTS idx_state_log_session_turn
           ON session_state_log(session_id, turn_id, retry_index)""",
        ],
    ),
    (
        14,
        "add workflow_runs and skill_improvement_proposals tables",
        [
            """CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id TEXT PRIMARY KEY,
            workflow_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            run_dir TEXT NOT NULL,
            step_count INTEGER NOT NULL DEFAULT 0,
            steps_passed INTEGER NOT NULL DEFAULT 0,
            steps_failed INTEGER NOT NULL DEFAULT 0,
            proposal_count INTEGER NOT NULL DEFAULT 0
        )""",
            """CREATE INDEX IF NOT EXISTS idx_wf_runs_name
           ON workflow_runs(workflow_name, started_at)""",
            """CREATE TABLE IF NOT EXISTS skill_improvement_proposals (
            id TEXT PRIMARY KEY,
            workflow_name TEXT NOT NULL,
            run_id TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            section TEXT NOT NULL DEFAULT '',
            problem TEXT NOT NULL,
            proposed_change TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            source_step_id TEXT NOT NULL DEFAULT '',
            source_worker_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_at TEXT DEFAULT NULL
        )""",
            """CREATE INDEX IF NOT EXISTS idx_proposals_skill
           ON skill_improvement_proposals(skill_name, status)""",
            """CREATE INDEX IF NOT EXISTS idx_proposals_workflow
           ON skill_improvement_proposals(workflow_name, run_id)""",
        ],
    ),
    (
        15,
        "session-origin proposals + trial-use tracking",
        [
            # Rebuild skill_improvement_proposals: relax NOT NULL on workflow_name/run_id,
            # add session_id, source_origin, trial_uses, trial_successes, last_trial_at.
            # SQLite can't ALTER COLUMN to drop NOT NULL, so we follow the documented
            # 12-step table rebuild (https://sqlite.org/lang_altertable.html).
            """CREATE TABLE skill_improvement_proposals_new (
            id TEXT PRIMARY KEY,
            workflow_name TEXT,
            run_id TEXT,
            session_id TEXT,
            source_origin TEXT NOT NULL DEFAULT 'workflow',
            skill_name TEXT NOT NULL,
            section TEXT NOT NULL DEFAULT '',
            problem TEXT NOT NULL,
            proposed_change TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            source_step_id TEXT NOT NULL DEFAULT '',
            source_worker_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            trial_uses INTEGER NOT NULL DEFAULT 0,
            trial_successes INTEGER NOT NULL DEFAULT 0,
            last_trial_at TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT DEFAULT NULL
        )""",
            """INSERT INTO skill_improvement_proposals_new
           (id, workflow_name, run_id, session_id, source_origin, skill_name,
            section, problem, proposed_change, confidence, source_step_id,
            source_worker_id, status, trial_uses, trial_successes, last_trial_at,
            created_at, resolved_at)
           SELECT id, workflow_name, run_id, NULL, 'workflow', skill_name,
                  section, problem, proposed_change, confidence, source_step_id,
                  source_worker_id, status, 0, 0, NULL,
                  created_at, resolved_at
           FROM skill_improvement_proposals""",
            "DROP TABLE skill_improvement_proposals",
            "ALTER TABLE skill_improvement_proposals_new RENAME TO skill_improvement_proposals",
            """CREATE INDEX IF NOT EXISTS idx_proposals_skill
           ON skill_improvement_proposals(skill_name, status)""",
            """CREATE INDEX IF NOT EXISTS idx_proposals_workflow
           ON skill_improvement_proposals(workflow_name, run_id)""",
            """CREATE INDEX IF NOT EXISTS idx_proposals_session
           ON skill_improvement_proposals(session_id, status)""",
            """CREATE INDEX IF NOT EXISTS idx_proposals_skill_status_conf
           ON skill_improvement_proposals(skill_name, status, confidence DESC)""",
        ],
    ),
    (
        16,
        "persist v2 state and watched_worker_ids on sessions for restart recovery",
        [
            # state_v2 stores the 10-state machine value directly. The legacy
            # `state` column mapped AWAITING_WORKERS / AWAITING_USER /
            # FINALIZING to "idle", losing crucial info across restart. With
            # state_v2 populated, get_or_create restores the true state.
            #
            # state_v2 is now the only session-state column the state machine
            # writes; `state` is retired but NOT dropped (see _SESSIONS_SCHEMA).
            # This ALTER deliberately does not backfill state_v2, so rows
            # stranded at state='processing' before this migration ran still
            # read NULL here — that is the entire reason
            # models.get_sessions_in_legacy_processing_only() exists.
            "ALTER TABLE sessions ADD COLUMN state_v2 TEXT",
            # watched_worker_ids is a JSON array (string '[]' default) holding
            # the IDs the parent is waiting on. Without this, restart loses the
            # watch-set and the parent silently waits for the reaper's 30-min
            # empty-set unstick instead of resuming when workers complete.
            "ALTER TABLE sessions ADD COLUMN watched_worker_ids TEXT NOT NULL DEFAULT '[]'",
            # Index for the boot-time reconcile sweep (small set in practice —
            # only sessions actively suspended on workers).
            """CREATE INDEX IF NOT EXISTS idx_sessions_state_v2
           ON sessions(state_v2) WHERE state_v2 IS NOT NULL""",
        ],
    ),
    (
        17,
        "add pinned flag on sessions for sidebar pinning",
        [
            "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
        ],
    ),
    (
        18,
        "add rlm_runs audit index for RLM (recursive processing) runs",
        [
            # Lightweight audit index for core/extensions/rlm runs. The heavy
            # data (trace.jsonl, staged context, child.log, answer.txt) lives
            # on disk at <workspace>/<run_dir>; run_dir is workspace-relative
            # (workflow_runs precedent). Rows and dirs are purged together by
            # snooze retention (rlm_run_retention_days). status='running' rows
            # surviving a restart are orphans — swept to 'orphaned' at boot.
            # parent_run_id/depth link nested rlm_query child runs to their
            # parent (NULL/0 for root runs). finished_at NULL = still running.
            """CREATE TABLE IF NOT EXISTS rlm_runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                parent_run_id TEXT,
                depth INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                task TEXT NOT NULL DEFAULT '',
                source_desc TEXT NOT NULL DEFAULT '',
                root_model TEXT NOT NULL DEFAULT '',
                sub_model TEXT NOT NULL DEFAULT '',
                iterations INTEGER NOT NULL DEFAULT 0,
                subcalls INTEGER NOT NULL DEFAULT 0,
                input_chars INTEGER NOT NULL DEFAULT 0,
                answer_preview TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                run_dir TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_rlm_runs_session
           ON rlm_runs(session_id, created_at DESC)""",
            "CREATE INDEX IF NOT EXISTS idx_rlm_runs_status ON rlm_runs(status)",
            "CREATE INDEX IF NOT EXISTS idx_rlm_runs_created ON rlm_runs(created_at)",
        ],
    ),
    (
        19,
        "add dream_hypotheses + dream_reports for idle-time introspection",
        [
            # Sidecar state for the dream add-on (core/dream, docs/dev/
            # dream-plan.md). Hypotheses reference their evidence by value
            # (evidence_json carries content hashes), never by annotating the
            # memory markdown — memory stays untouched. status lifecycle:
            # pending -> validated | refuted | expired -> promoted | archived.
            # Dropping both tables is safe (nothing else joins to them);
            # watermarks live in snooze_state under dream_* keys.
            """CREATE TABLE IF NOT EXISTS dream_hypotheses (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                statement TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                confidence REAL NOT NULL DEFAULT 0.0,
                validation_json TEXT,
                promoted_ref TEXT,
                origin TEXT NOT NULL DEFAULT 'dream_cycle',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_dream_hyp_status
           ON dream_hypotheses(status, created_at DESC)""",
            "CREATE INDEX IF NOT EXISTS idx_dream_hyp_kind ON dream_hypotheses(kind, status)",
            """CREATE TABLE IF NOT EXISTS dream_reports (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                path TEXT NOT NULL,
                stats_json TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_dream_reports_created ON dream_reports(created_at DESC)",
        ],
    ),
    (
        20,
        "link rlm_runs to their sidebar view sessions (ui_session_id)",
        [
            # ui_session_id points at the session_type='rlm' pseudo-session
            # that anchors the run in the sidebar under its parent chat. The
            # session is pure navigation chrome — zero messages; the run's
            # content stays in trace.jsonl on disk. NULL for runs with no UI
            # surface: dream probes, nested rlm_query children, pre-v20 rows.
            # Deleting either side must clean up the other (manager.delete_session
            # purges run dir + rows; snooze retention deletes the session).
            "ALTER TABLE rlm_runs ADD COLUMN ui_session_id TEXT",
            """CREATE INDEX IF NOT EXISTS idx_rlm_runs_ui_session
           ON rlm_runs(ui_session_id) WHERE ui_session_id IS NOT NULL""",
        ],
    ),
    (
        21,
        "cron claim-before-deliver (fire_time on cron_runs)",
        [
            # fire_time = the scheduled tick this run was claimed for, written
            # BEFORE dispatch so a crash mid-run can never replay the prompt:
            # the startup reconcile marks claimed/running rows 'uncertain' and
            # never re-sends. Adaptation plan 1c.
            "ALTER TABLE cron_runs ADD COLUMN fire_time TEXT",
        ],
    ),
    (
        22,
        "deterministic gates (adaptation plan 3a)",
        [
            # A gate is a user-authored shell command whose exit code is
            # host-observable evidence Reflect cannot overrule: any failing
            # gate clamps a pass verdict to retry. watch_paths (JSON list)
            # scopes the unchanged-workspace reuse guard — the global
            # workspace churns from unrelated sessions, so a whole-tree
            # fingerprint would be meaningless.
            """CREATE TABLE IF NOT EXISTS gates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'session',
                name TEXT NOT NULL,
                command TEXT NOT NULL,
                watch_paths TEXT,
                cwd TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT,
                UNIQUE(session_id, name)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_gates_session ON gates(session_id, enabled)",
        ],
    ),
    (
        23,
        "persistent goals (adaptation plan 3b)",
        [
            # One active goal per session (enforced in the accessor). Only
            # goal_complete reaches status=complete. token_usage.goal_id is
            # stamped at write time — including worker rows, which bill to
            # their own session_id but inherit the parent's goal — so budget
            # accounting is a flat SUM, no parent rollup needed.
            """CREATE TABLE IF NOT EXISTS session_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                token_budget INTEGER,
                time_budget_s INTEGER,
                continuation_budget INTEGER DEFAULT 0,
                continuations_used INTEGER DEFAULT 0,
                started_at TEXT,
                updated_at TEXT,
                completed_at TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_session_goals ON session_goals(session_id, status)",
            "ALTER TABLE token_usage ADD COLUMN goal_id INTEGER",
        ],
    ),
    (
        24,
        "golden-task canary suite (adaptation plan 3.5)",
        [
            # One row per canary run. batch_id is a real nullable column —
            # the Phase 4 tripwire joins post-batch sweeps against the
            # adaptive batch that triggered them; smuggling it into a string
            # would make that join impossible. gate_results_json carries the
            # FINAL attempt's per-gate payloads; retries records how many
            # reflect retries the run burned.
            """CREATE TABLE IF NOT EXISTS canary_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL,
                trigger TEXT NOT NULL DEFAULT 'manual',
                batch_id TEXT,
                session_id TEXT,
                gate_results_json TEXT,
                passed INTEGER,
                retries INTEGER DEFAULT 0,
                tokens INTEGER DEFAULT 0,
                duration_s REAL DEFAULT 0,
                created_at TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_canary_runs_task ON canary_runs(task, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_canary_runs_batch ON canary_runs(batch_id)",
        ],
    ),
    (
        25,
        "adaptive layer (adaptation plan 4a)",
        [
            # Machine-managed policy store, DB-first unlike memory: version-
            # chained rows with full-snapshot event history — hand-editing a
            # version chain corrupts rollback, so the markdown mirror
            # (data/adaptive/ADAPTIVE.md) is render-only, never read back.
            """CREATE TABLE IF NOT EXISTS adaptive_entries (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global',
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                risk TEXT NOT NULL DEFAULT 'low',
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_adaptive_entries_kind ON adaptive_entries(kind, status)",
            # Append-only journal. The autoincrement id is the rollback
            # ordering key — created_at text timestamps are not monotonic
            # within a batch.
            """CREATE TABLE IF NOT EXISTS adaptive_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL,
                action TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                evidence_json TEXT,
                actor TEXT,
                proposal_id INTEGER,
                batch_id TEXT,
                created_at TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_adaptive_events_batch ON adaptive_events(batch_id)",
            "CREATE INDEX IF NOT EXISTS idx_adaptive_events_entry ON adaptive_events(entry_id)",
            # payload_json holds a pending batch's edits until the idle-window
            # drain applies them ([IMPL] addition to §6 — the pending queue
            # needs a home and the batch row is its natural one).
            """CREATE TABLE IF NOT EXISTS adaptive_batches (
                batch_id TEXT PRIMARY KEY,
                producer TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payload_json TEXT,
                flagged_reason TEXT,
                cleared_at TEXT,
                created_at TEXT
            )""",
            # Apply-on-approve proposals — NOT skill_improvement_proposals,
            # which is skill-shaped and whose approve is a bare status flip.
            """CREATE TABLE IF NOT EXISTS adaptive_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                producer TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                evidence_json TEXT,
                rationale TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                resolved_at TEXT,
                created_at TEXT
            )""",
        ],
    ),
    (
        26,
        "one-active-goal uniqueness (backstop for create_goal's check-then-insert)",
        [
            # v23 documented "one active goal per session (enforced in the
            # accessor)" but shipped only a NON-unique index, and the accessor's
            # SELECT ran in autocommit (legacy isolation_level begins a
            # transaction only before DML) — so two concurrent create_goal calls
            # both read "no active goal" and both inserted. create_goal now takes
            # BEGIN IMMEDIATE around the check; this index is the backstop that
            # makes the invariant true regardless of which caller races.
            # Existing duplicates would fail the CREATE, so retire the older ones
            # first: get_active_goal already resolves ties with ORDER BY id DESC,
            # so the highest id per session is the one that has been live.
            """UPDATE session_goals SET status = 'error',
                   updated_at = COALESCE(updated_at, started_at),
                   completed_at = COALESCE(completed_at, updated_at, started_at)
               WHERE status IN ('active', 'paused', 'budget_limited')
                 AND id NOT IN (
                   SELECT MAX(id) FROM session_goals
                   WHERE status IN ('active', 'paused', 'budget_limited')
                   GROUP BY session_id
                 )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_session_goals_one_active
                   ON session_goals(session_id)
                   WHERE status IN ('active', 'paused', 'budget_limited')""",
        ],
    ),
    (
        27,
        "background jobs table (job_start/job_status/job_tail/job_kill)",
        [
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                command TEXT NOT NULL,
                pid INTEGER,
                state TEXT NOT NULL DEFAULT 'running',
                exit_code INTEGER,
                created_at TEXT NOT NULL,
                deadline_s INTEGER NOT NULL DEFAULT 0,
                finished_at TEXT,
                log_path TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_jobs_session
                   ON jobs(session_id, created_at)""",
        ],
    ),
    (
        28,
        "questions become an audit trail (answer + answered_at; rows kept)",
        [
            "ALTER TABLE questions ADD COLUMN answer TEXT",
            "ALTER TABLE questions ADD COLUMN answered_at TEXT",
        ],
    ),
    (
        29,
        "rlm_runs learn whether their outcome ever reached the agent "
        "(orphan surfacing; history backfilled as already-seen)",
        [
            "ALTER TABLE rlm_runs ADD COLUMN surfaced_at TEXT",
            """UPDATE rlm_runs SET surfaced_at = COALESCE(finished_at, created_at)
               WHERE status != 'running'""",
        ],
    ),
    (
        30,
        "canary run outcomes: separate timeout/error/noop from honest gate failures",
        [
            # outcome ∈ pass | gate_fail | timeout | error | noop. Before this,
            # a run killed at its wall clock and a run that genuinely failed
            # its gates both landed as passed=0 — the tripwire and suite-health
            # heuristics had to reverse-engineer the difference from tokens
            # and duration. Legacy failed rows where that distinction is
            # unrecoverable keep outcome NULL; consumers must treat NULL
            # failures conservatively (they never feed the per-task tripwire).
            "ALTER TABLE canary_runs ADD COLUMN outcome TEXT",
            "ALTER TABLE canary_runs ADD COLUMN error TEXT",
            "UPDATE canary_runs SET outcome = 'pass' WHERE passed = 1",
            """UPDATE canary_runs SET outcome = 'noop'
               WHERE passed = 0 AND IFNULL(tokens, 0) = 0 AND IFNULL(duration_s, 0) < 1.0""",
        ],
    ),
    (
        31,
        "resumable workers: persist a worker's pinned model and typed kind "
        "so rehydration after a reap/restart restores its identity",
        [
            # model_override / worker_kind were in-memory only, so a worker
            # rehydrated from the DB (server restart, idle reap) silently
            # lost its pinned model and its kind's tool allowlist. NULL =
            # no override / untyped worker (every pre-v31 row).
            "ALTER TABLE sessions ADD COLUMN model_override TEXT",
            "ALTER TABLE sessions ADD COLUMN worker_kind TEXT",
        ],
    ),
    (
        32,
        "re-armable refine watermarks: convert refined:{sid} snooze_state "
        "values from ISO timestamps to the session's max message id",
        [
            # Old semantics: 'refined once, never again' (value = when).
            # New semantics: 'refined up to message N' — a session that
            # grows past N becomes eligible again, so refine can revisit a
            # session whose interesting half (the workaround) happened
            # after its first pass. Converting to the CURRENT max id means
            # 'processed up to now' for every legacy row: nothing already
            # graded re-runs on deploy, only future growth re-arms.
            """UPDATE snooze_state
               SET value = CAST(COALESCE(
                       (SELECT MAX(m.id) FROM messages m
                        WHERE m.session_id = substr(snooze_state.key, 9)),
                       0) AS TEXT)
               WHERE key LIKE 'refined:%'""",
        ],
    ),
    (
        33,
        "spaces: named/colored long-lived session groups",
        [
            # slug is immutable after creation — the memory-file prefix
            # (pernix.space.<slug>.*), the directives dir (data/agent/spaces/
            # <slug>/) and the workspace home (data/workspace/spaces/<slug>/)
            # all key off it, so a label rename must never move files.
            """CREATE TABLE IF NOT EXISTS spaces (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#7c9cff',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "ALTER TABLE sessions ADD COLUMN space_id TEXT",
            "CREATE INDEX IF NOT EXISTS idx_sessions_space ON sessions(space_id) WHERE space_id IS NOT NULL",
        ],
    ),
]


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """Get current schema version. Returns 0 if no schema_meta exists."""
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Set current schema version."""
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending versioned migrations.

    Each migration runs in its own explicit transaction with the version
    bump included — DDL is transactional in SQLite, but on a default-mode
    connection it autocommits immediately while the version bump waits for
    the final commit. A crash between two statements (e.g. v16's pair of
    ALTER TABLEs) then left half-applied DDL that re-ran on next boot and
    died with "duplicate column name", bricking startup until manual SQL
    surgery. BEGIN..COMMIT per migration makes a crash roll back wholesale.
    """
    current = _get_schema_version(conn)
    applied = 0
    prev_isolation = conn.isolation_level
    conn.commit()  # flush any pending implicit transaction before switching modes
    conn.isolation_level = None  # explicit transaction control
    try:
        for version, description, statements in MIGRATIONS:
            if version <= current:
                continue
            logger.info("Applying migration v%d: %s", version, description)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for sql in statements:
                    conn.execute(sql)
                _set_schema_version(conn, version)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            applied += 1
    finally:
        conn.isolation_level = prev_isolation
    if applied:
        logger.info("Applied %d migration(s), now at v%d", applied, _get_schema_version(conn))


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def _check_integrity(conn: sqlite3.Connection, label: str) -> None:
    """Run PRAGMA quick_check for corruption detection."""
    result = conn.execute("PRAGMA quick_check").fetchone()
    status = result[0] if result else "unknown"
    if status != "ok":
        logger.critical("Database integrity check FAILED for %s: %s", label, status)
    else:
        logger.debug("Integrity check passed: %s", label)


def init_sessions_db() -> None:
    """Initialize the sessions database with schema and migrations."""
    conn = connect_sessions()
    try:
        conn.execute("PRAGMA auto_vacuum=2")  # INCREMENTAL
        conn.executescript(_SESSIONS_SCHEMA)
        # Set initial version if fresh DB
        if _get_schema_version(conn) == 0:
            _set_schema_version(conn, 1)
            conn.commit()
        _run_migrations(conn)
        # Reclaim messages_fts rows orphaned by the pre-fix delete_session
        # (it relied on ON DELETE CASCADE, which never touches the FTS table).
        # Indexed anti-join — cheap once the backlog is cleared.
        try:
            cur = conn.execute("DELETE FROM messages_fts WHERE rowid NOT IN (SELECT id FROM messages)")
            if cur.rowcount:
                logger.info("Reclaimed %d orphaned messages_fts rows", cur.rowcount)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # FTS table may not exist yet on a partially-initialized DB
        _check_integrity(conn, "sessions")
    finally:
        conn.close()


def init_memory_db() -> None:
    """Initialize the memory FTS5 database."""
    conn = connect_memory()
    try:
        # Schema upgrade: memory_fts gained source/updated UNINDEXED columns.
        # The index is rebuildable from markdown, so upgrading is drop +
        # recreate; the startup health check (fix=True) repopulates it.
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_fts)")}
        except sqlite3.OperationalError:
            cols = set()
        if cols and "source" not in cols:
            conn.execute("DROP TABLE memory_fts")
            conn.commit()
            logger.info("memory_fts schema upgraded (+source/updated); index rebuilds from markdown")
        conn.executescript(_MEMORY_SCHEMA)
        _check_integrity(conn, "memory")
    finally:
        conn.close()


def init_db() -> None:
    """Initialize all databases and directories."""
    ensure_dirs()
    init_sessions_db()
    init_memory_db()
    logger.info("Database initialization complete")
