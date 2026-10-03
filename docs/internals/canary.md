# Canary Suite — Measured Self-Checks

> **Notifications.** Every "notification" this page mentions goes through `core/notices.py` and lands in the tier its category is registered under — most self-maintenance receipts are *log* tier (the bell's Activity tab, never a badge); only things that need you interrupt. See [guides/notifications.md](../guides/notifications.md) for the full list.

The **canary suite** (`core/canary/`) is Pernix's measurement substrate:
golden tasks with deterministic gates, run headlessly through the full
pipeline, answering the question no ledger of anecdotes can — *is the agent
actually getting better or worse?* It complements the observation half of
self-improvement — post-mortems, scout signals, [Dream](dream.md), the refine
pass.

Off by default (`canary_enabled`) and inert when off: zero rows written.

**Change-driven, small, relevant (3.2).** The suite runs after a deploy,
after a model swap, and when you press Run — never on a wall clock. It is
four hand-curated, generated canaries; it no longer grows, parks, promotes
or retires itself, and a contaminated run is a record, not an alarm. Until
3.2 the suite was also paired with the **adaptive layer**, whose tripwire
read post-batch canary runs; that layer is retired (its tables stay as
history; see [upgrade.md](../upgrade.md#whats-gone-in-32)).

---

## The canary suite

### What a canary is

One directory per canary under `data/canaries/`, each holding a `CANARY.md`:
frontmatter defining the task, a markdown body of free-form notes for the
humans who review it. A full example:

```markdown
---
name: fix-failing-test
prompt: |
  The test in tests/test_math.py fails. Find the bug and fix it.
gates:
  - name: pytest
    command: python -m pytest tests/test_math.py -q
    watch_paths: [src/]
files:
  src/mathlib.py: |
    def add(a, b):
        return a - b   # the planted bug
  tests/test_math.py: |
    from src.mathlib import add
    def test_add():
        assert add(2, 2) == 4
model: ""            # optional model override
timeout: 600         # optional per-run wall clock (seconds)
tags: [coding, debug]
covers: []           # informational: change surfaces it tests, e.g. [skill:foo]
flaky: false         # flaky canaries inform, never count as failures
serve: []            # seed files served over HTTP instead (see below)
tools: []            # extra read-only web tools: http_get, browse_web
last_reviewed: 2026-08-06
---
Checks that the agent can localize a one-line arithmetic bug from a failing
test and fix it without breaking the test file.
```

`name`, `prompt`, and a non-empty `gates` list are required — a canary
without gates cannot be scored. The optional `files:` map seeds the run's
workspace with deterministic fixtures (workspace-relative paths only), so
canaries are self-contained: fixtures over live URLs, per the flakiness
discipline. Gates locally deterministic; anything that can't be, tag `flaky`
— flaky canaries inform but never count as failures. `covers:` names what a
canary exercises and is shown in the tab; nothing selects on it any more.
`parked`, `max_runs`, `expires` and `cadence` belonged to the retired
auto-maintenance: they still parse, and nothing reads them. Invalid files log
a warning and are skipped; one bad canary never sinks a sweep.

### How a run works

Each canary runs as a headless `session_type="canary"` session through the
**full pipeline** — scout → agent → gates → reflect — because a canary that
skips what real turns exercise measures nothing. The workspace is a temp
directory per run (seeded from `files:`), the gates materialize as
canary-scoped rows for the run and are deleted after, and the score is the
gates re-run against the final workspace state. A run that triggered reflect
retries scores the final attempt; the retry count is recorded.

Every run records an **outcome**: `pass`, `gate_fail` (the agent ran and
the work was wrong), `timeout` (killed at the wall clock), `error` (the
harness broke), or `noop` (zero tokens, sub-second — the agent never
executed). Only `gate_fail` is evidence about the agent; the rest are
suite-health trouble. Results land in the
`canary_runs` table (gate results, outcome, error, pass/fail, retries,
tokens, duration) and in the Explorer's **Self-tuning → Self-checks (Canary)**
tab.

### Isolation guarantees

Canary sessions are deliberately hard synthetic tasks; letting them leak into
the stores that shape live behavior would poison the very signals they exist
to guard. The isolation is an enumerated predicate list, not a vibe:

- **No memory at all** — writes *and* reads. The memory-write tools are
  denied to the session type; `recall`/`deep_recall` are off the allowlist
  and the scout does no memory preload (see *Canary isolation* below).
- **Invisible to search** — canary messages are excluded from session FTS.
- **Excluded from distill/refine sweeps**.
- **Post-mortems are written but stamped** `session_type='canary'` and
  excluded from synthesis and model-routing aggregation.
- **Snooze-transparent** — canary sessions neither cancel a snooze cycle nor
  block its idle gate, so a sweep and idle housekeeping coexist.
- **Hidden from the session sidebar** like Dream journals.
- **Tool-allowlisted** — every canary session runs under
  `CANARY_TOOL_ALLOWLIST` (computation and workspace reads only: file/search/
  repl tools plus read-only skill and tool discovery), enforced at the same
  three points as scheduled-job charters. A canary may load a skill whose
  SKILL.md asks for mutating actions, so workers, jobs, notifications, and
  every skill/tool/memory mutation are fenced off for the whole session
  type. A canary may add `http_get` / `browse_web` with `tools:` (see
  *Served fixtures* below) and nothing else.

### Canary isolation

The suite exists to measure one thing: how the pipeline performs **under the
treatment** — the skills and tools in force right now. Anything
that lets a canary read the learning stores measures the stores instead, and
anything that lets the learning stores read a canary poisons the live agent
with synthetic, deliberately-hard transcripts. The 2026-09-04 hardening
audited every path in both directions and turned the list into assertions
(`tests/test_canary_isolation_hardening.py`).

**Nothing learns from a canary session.**

| Path | Guarantee |
|---|---|
| Memory distillation | `distill_session` returns immediately for `session_type == "canary"` — the guard is on the funnel, not only on `sessions/hooks._maybe_distill`, so no future caller can reopen it. The snooze catch-up selector excludes the type in SQL. |
| Refine | `run_for_session` skips with `skipped_reason="canary_session"`, so no lesson and no SKILL.md proposal is ever derived from a scored run. `db.get_unrefined_sessions` excludes the type as well. |
| User-profile sweep | Snooze's insight extraction excludes canary sessions (it excluded workers only until W5). |
| Distill-coverage audit | Excludes the type in SQL. |
| Dream observation, model-routing aggregation | Early-return on the type; post-mortems are written but stamped `session_type='canary'` and skipped by synthesis. |
| Space suggestions | The candidate query is `session_type = 'normal'`. |
| Auto-title | The runner names every session `Canary: <name>` at creation, and the titler only fires on sessions still called `New session`. |
| Cross-session search | `search_messages_fts` excludes canary rows, so a canary transcript cannot surface in another session's scout. |
| Retention | Canary sessions are pruned with no distill-before-delete digest. |

**No canary session learns from us.**

| Path | Guarantee |
|---|---|
| Scout preload | For a canary brief, the memory baseline, deep-memory, cross-session and lessons gatherers all return `None` (`scout.runner.memory_recall_denied`). The non-memory preload — tools, skills, models, workspace state — is the treatment and stays. |
| Scout tools | `search_memory` is removed from the scout's tool schema, and `_exec_scout_tool` refuses it as a backstop. |
| Scout fallback | The deterministic fallback report skips its `store.recall` too. |
| Agent tools | `recall` and `deep_recall` are off `CANARY_TOOL_ALLOWLIST`; the memory-write tools were already denied by `denied_session_types`. |
| The answer key | `list_gates` is off the allowlist. It prints each gate's command verbatim, and a canary gate command *is* the expected answer (`grep -qx '13' answer.txt`) — one tool call used to turn every scored task into an open-book exam. |

What a canary deliberately *keeps*: bash, the file/search/repl tools, skill
discovery and loading. Those are the treatment under measurement, not
contamination.

#### The holdout rule

A canary tagged `holdout` is a task the learning loop is never allowed to
see. Nothing renders canary names into a refine or dream prompt today;
`core.canary.prompt_safe_canaries()` is the list anything that starts must
use, and a test fails the day a holdout name appears in one. Since 3.2 no
producer writes canaries at all, so the proposal-side checks (resemblance to
a holdout, refusing a lookalike) went with the self-growth path. All four
shipped canaries are tagged `holdout`.

### Generated fixtures

A saturated suite proves nothing, and a hand-written canary ships its answer
in the repository: the retired `grep-count` expected `8`, in the gate
command, forever. A model that has seen the transcript once passes it without
doing the work.

So a canary directory may carry a `generate.py` beside its `CANARY.md`:

```python
def generate(seed: int) -> dict:
    return {
        "prompt": "...",                                     # the task
        "files": {"logs/app.log": "..."},                    # workspace seed
        "gates": [{"name": "...", "command": "grep -qx '13' answer.txt",
                   "watch_paths": ["answer.txt"]}],          # the scoring
    }
```

Such a `CANARY.md` omits `prompt`, `gates` and `files` and carries
`generated: true`; everything else (name, timeout, tags, flaky, covers,
serve, tools) is read normally. The runner draws a fresh seed per run
(`core/canary/fixtures.py`), calls `generate(seed)` in-process — the same
trust level as the gate shell commands it produces, so generated canaries are
a hand-authored, repository-reviewed surface — and takes the prompt, the seed
files and the gates from that one call.

**Where the expected value lives:** only inside the gate command, built at run
time. Not in the workspace (input files only), not in the prompt, not in a
tool result — `list_gates` is off the canary allowlist precisely because it
printed gate commands verbatim. What *is* persisted is the seed, appended to
`gate_results_json` as `{"seed": n, "generated": true}`: enough to reproduce a
failed run by hand (`generate(seed)`), useless to a model that memorised last
week's answer. A rerun therefore runs on a *different* fixture, which makes
a repeated failure stronger evidence, not weaker.

All four shipped canaries are generated (see *The suite* below). Each
generator's expected values are checked against its own rendered fixture
across 20 seeds in `tests/test_canary_isolation_hardening.py` — the gates
must pass a correct answer and fail a near miss (an off-by-one, the decoy
figure, an uncollapsed transcript).

### Served fixtures and scoped web tools

A canary that tests reading a link cannot depend on the internet, so it can
serve its own page:

- **`serve: [article.html]`** names seed files (from `files:` or from
  `generate()`) that the runner publishes under
  `<workspace_dir>/.canary-serve/<run-token>/` for the length of the run
  instead of writing them into the task workspace. `{{SERVE_BASE}}` in the
  prompt **and** in gate commands becomes
  `<scheme>://localhost:<port>/workspace/.canary-serve/<run-token>` — Pernix's
  own `GET /workspace/{path}` route (https when `network_enabled`, else
  http). The directory is removed in the runner's `finally`, whatever the
  turn did. Loopback requests pass auth under `trust_local_requests`, and the
  SSRF guard already allows the server's own port.
- **`tools: [http_get]`** adds tools to the run's allowlist, only from
  `DECLARABLE_TOOLS` = {`http_get`, `browse_web`}. Anything else — `search_web`
  included — is a parse error, and the runner intersects again before
  granting. Nothing machine-written emits this key.
- **Own-certificate TLS.** In network mode the server's certificate is
  self-signed (SAN `localhost`, `127.0.0.1`) or custom. `http_get` chooses TLS
  verification per hop: https to `localhost` or `127.0.0.1` on exactly
  `settings.port` verifies against Pernix's own certificate file (a context
  whose only trust anchor is that cert, hostname checking on); every other URL
  keeps the default CA bundle. Verification is never switched off. A custom
  certificate whose SAN lacks `localhost` will fail this check — the canary
  then records a gate_fail, not a pass. `browse_web` is unchanged: Playwright
  cannot pin a CA, so it cannot read the own https host, and `link-digest`
  declares `http_get` only.

### The contamination scan

Isolation enforced at three points is exactly the kind of claim that quietly
stops being true — a new tool ships without `denied_session_types`, an MCP
server exposes a memory-shaped verb, an injected `SKILL.md` steers the agent
out of its workspace — and the suite keeps reporting green while measuring
something other than the pipeline. So every run is read back afterwards
(`core/canary/contamination.py`) and asked three questions:

1. Did it call a **memory tool**? It has none on its allowlist, so one that
   answered means the fence has a hole.
2. Did a tool argument name an **absolute path outside the temp workspace**?
   Toolchain paths (`/usr`, `/bin`, `/etc`, …), the skills directory (a
   canary may load the skill it tests) and `.canary-serve/` do not count;
   Pernix's own data directory, another session's files, or the repository
   do. A single-segment token (`/ERROR`, `/1000`, `/512`) is prose or
   arithmetic far more often than a path, so it counts only when it names a
   known data root (`/app/data`, `data/memories`, `sessions.db`).
3. Does the transcript or a tool argument point at **`data/canaries`** or at
   **another canary's directory** (`canaries/<name>`)? That is the suite
   reading its own answer key. Another canary's bare *name* is not a
   finding: `gen-grep-count` contained `grep-count`, and every such run used
   to be disqualified.

A hit sets `canary_runs.outcome = 'contaminated'`. It is a **record, not an
alarm**: no notification is raised (on the reference box almost every
contaminated run was a false positive of the path heuristic, and each one
was a notice). The scored `passed` value is preserved exactly as the gates
returned it — the run is not rewritten, it is **disqualified**: the tab and
the Trust tab count it apart from passes and failures, so a compromised run
cannot vouch for the pipeline. The finding is appended to `gate_results_json` as an `isolation` row so the tab
can say why. Detection, not prevention: bash is on the allowlist because the
seed tasks need it, so the workspace is a fence — the scan is what makes it
observable.

### Triggers — change-driven, never wall-clock

Until 3.2 the suite also ran a nightly heartbeat, post-batch probes after
adaptive applies, and targeted sweeps on skill edits; in its last month on
the reference box that was 361 runs and ~23% of all recorded tokens, at a
92% pass rate. Now it runs only when the world changed or you ask:

| Trigger (recorded) | When |
|---|---|
| `deploy` | The boot version stamp changed. **Debounced**: the full sweep is queued 15 minutes after boot under one job id, and a restart inside that window replaces the queued job, so a burst of rebuilds is measured once. |
| `model-swap` | `llm_model` changed, through either switch path (`POST /api/models/switch`, the settings endpoint). Queued one minute later. |
| `manual` | The tab's **Run** (one canary) and **Run all** (`POST /api/canary/run`, `"*"` for all). |

Full sweeps (deploy, model swap, Run all) carry `must_run`, so a sweep in
flight defers them instead of eating them (the lock is otherwise
skip-not-queue), and each reports **once** when it finishes: a canary that
gate-failed raises `canary.sweep_failed` (a quiet bell item, coalesced; a
sweep where everything passed resolves it), anything else is a
`canary.sweep_result` line in Activity. Timeouts, harness errors and
contaminated runs never ring the bell. Rows written before 3.2 carry
`scheduled`, `full` or `post_batch` and stay readable. The agent has the
read-only `canary_status` tool; it cannot start a run (`canary_run` was
retired). One sweep runs at a time; Snooze prunes runs past
`canary_retention_days` (30).

### The suite

`data/canaries/` is git-tracked and hand-curated. **Run all executes
exactly these four**, all generated per run and tagged `holdout`:

| Canary | Covers |
|---|---|
| `gen-file-create` | Instruction following: one named file, one exact line, a near-miss decoy in the workspace. |
| `gen-json-transform` | Read, filter, aggregate, emit JSON, with a customer that never ships as the trap. The prompt asks for the REPL when the session has it. |
| `link-digest` | Reading a link: a served article with a random title, author and key figure (plus a revised decoy figure), fetched with `http_get`; gates check all four facts and the source URL in `summary.md`. |
| `youtube-captions-digest` | The youtube-whisper skill's caption-first path on local files: an `.info.json` and an auto-caption `.vtt` with an empty cue, a tags-only cue and rolling duplicates. Gates: both outputs exist, the seeded key sentence appears exactly once in `transcript_clean.txt`, the title is in `summary.md`. The skill lives in `data/skills/` (not in the repository); where it is missing the prompt tells the agent to stop, so the run is a gate_fail. |

What is deliberately **not** a canary: the memory round trip (canary
sessions are denied memory tools by design — it is
`tests/test_memory_roundtrip.py`), workers and `search_web` (unit tests).

### Lifecycle

The tab and the API cover the whole lifecycle by hand: create (raw
CANARY.md or a structured spec; gate commands are checked against an
allowlist proof and the verdicts returned as *warnings*, never blockers;
a spec whose task points outside the sandbox is refused), edit (`PUT`,
validated round-trip), mark reviewed, and retire (`DELETE` — the directory
moves to `.retired/`, and Snooze's retention rung (12c) deletes it for good
after `canary_purge_after_days`, so a retirement is reversible for the
whole window). Nothing mutates the suite on its own: the auto-admission,
vetting, flap tagging, parking, one-off probe retirement, suite-health
alerts and the 90-day staleness nudge were retired in 3.2.

**Skill verify blocks.** A skill's `verify:` frontmatter block used to be
materialised as a managed canary `skill--<name>` and fed the automatic skill
rollback. Both were removed in 3.2 (with `core/canary/skill_verify.py`); the
block is ignored. Manual skill rollback (`POST
/api/skills/proposals/{id}/rollback`, the Skills tab) is unaffected.

---

## The Trust tab

The Explorer's **Self-tuning** group carries a second tab, **Trust**, beside
Self-checks. Self-checks shows the suite doing its job; Trust answers the
question underneath it — how much of the grading is grounded in something
that happened, and how much is the model agreeing with itself. It reads
`GET /api/trust` and renders counts only, no charts:

- **Grader agreement** — how often reflect's verdict matches the user's thumbs
  on the same turn, over the number of turns that carry both; plus the
  grader's hold-out accuracy against the fixture set when a run has recorded
  one.
- **Where outcomes come from** — turns whose outcome came from a thumbs, from
  the user's next message, and from reflect alone, in that precedence order,
  plus turns graded against turns sent in the last 7 days.
- **Self-checks (14 days)** — runs, failures, and runs marked contaminated
  (one that reached memory or a file outside its own workspace, and is
  therefore excluded from every measurement).

The tab is read-only apart from its Refresh button, and every field is
optional: a subsystem that is off reports zeros. On a server that predates the
trust loop the endpoint is a 404 and the tab is one line saying so.

## Settings

See [configuration.md](../configuration.md#canary-suite). API surface:
[api.md](../api.md#canary-suite).
