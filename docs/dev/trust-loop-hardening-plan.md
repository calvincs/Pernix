# Trust-loop hardening plan (2026-09-04)

Branch: next-3.2-testing. Baseline audit: hkb `pernix.audit.learning-loop-2026-09`.

## The problem in one line

Pernix stores what happened, but the loop that is supposed to turn history into better
behaviour has no ground truth, no controlled measurement, and no reliable undo. Reflection
grades its own homework, adaptations are prose the model may ignore, canaries are saturated
and can be contaminated by the memory they are meant to test.

## Principles (what "trustworthy" means here)

1. **Receipts, not stories.** No entry becomes permanent without a reference to a recorded
   outcome (a post-mortem, a candor fact, a signal row, a user reaction). LLM text citing
   other LLM text is not evidence.
2. **Ground truth outranks self-grading.** Outcome precedence per turn:
   `user` (explicit thumbs) > `next_turn` (the user's next message) > `llm` (reflect verdict).
   Every outcome carries its `outcome_source` so the share of grounded outcomes is visible.
3. **Every adaptation is an experiment.** New prompt entries enter a trial arm, are rendered
   on a deterministic half of turns, and are promoted or retired on measured outcome
   difference, never on the veto clock alone.
4. **Measurement needs a sample and a test**, not a 20-turn ratio. Two-proportion tests with
   minimum n; the destructive action (retire/rollback) uses the stricter alpha.
5. **Eval data stays out of memory, memory stays out of eval.** Canary sessions run with the
   treatment (adaptive entries, skills) but without memory recall; canary transcripts are
   never distilled, refined, or proposed from; sentinel fixtures are generated per run so a
   memorised answer cannot pass.
6. **Every channel has an undo.** Adaptive batches (exists), skill auto-apply (new), and the
   tripwire can trigger it on measured regression.
7. **Signal, not activity.** One `/api/trust` surface reports grader agreement, outcome-source
   mix, trial results, contamination count, unfounded entries.

## What is deliberately deferred (and why)

- Telos objective evaluation: a design decision (restore goal binding vs rename). Not hardening.
- Flipping learning defaults to on: product decision for Calvin.
- Dream memory-correction reversibility: additive entries; needs a delete path design.
- Canary `covers:` tags: superseded by trial arms as the effect measurement; canaries remain
  the regression floor.

## Batch 1 (five parallel workstreams, one worktree each)

### W1 Attribution honesty (owner: core/synthesis.py, core/scout/runner.py use-bump, core/tools/builtin/dialog_tools.py, core/tools/executor.py, core/extensions/candor/emit.py, core/snooze.py candor-hint block)

1. `cited_policies` and `used_hints` get a failure branch: verdict in {retry, escalate} with
   `failure_cause` in {agent, scout} adds `delta_failures=1`; env/task causes skip. Keep the
   existing success branch. Read reflect's failure_cause taxonomy in core/reflect.py first.
2. Do not bump a hint's "use" at scout time for canary sessions or fallback-model plans
   (mirror the filters in `attribute()`), so uses cannot accrue without outcomes.
3. By-design unavailability is not a failure. `ask_user` (and any dialog tool) in an
   unattended session returns an informational, non-error result. Introduce one marker the
   executor understands (for example a `ToolResult.unavailable` flag or a fixed prefix
   `[unavailable]`) so `was_error` is False, `tool_summary` failures do not count it, and
   candor emits no `tool_ok=false` for it.
4. Candor hint producer (core/snooze.py candor block): exempt dialog tools from "degraded"
   hints by name (ask_user, notify-style tools), and stamp a structured receipt
   `candor:<fact_key>` into the edit's evidence list (see W4 receipt format).
5. Tests: dated regression tests (`tests/test_attribution_2026_09_04.py`, etc.).

### W2 Ground-truth backend (owner: sessions/hooks.py deferred path, core/reflect.py evidence + prompt, db/database.py migration v36, db/models.py, new core/feedback.py, new api/routers/trust.py, api/routers/sessions.py feedback routes, core/metrics.py)

1. **Next-turn grading.** When a real user turn N+1 starts while turn N's deferred grade is
   pending, grade turn N anyway with the first user message of N+1 as evidence
   ("USER'S NEXT MESSAGE"), slicing turn N's transcript by the message-id range captured at
   schedule time. Prompt rule: a next message that corrects, repeats the request, or
   complains means the turn missed intent (non-pass, cause agent); a message that moves on or
   thanks is evidence of pass. Deterministic pre-check `next_msg_correction` (regex list) is
   stored in the payload regardless of the LLM's reading. Every real turn gets graded unless
   the session is destroyed; bound cost with one in-flight deferred grade per session and a
   queue. Keep the 300 s idle grade for turns with no reply. Flag `reflect_next_turn_grading`
   (default True).
2. **Migration v36:** table `message_feedback(id, session_id, message_id UNIQUE, signal
   TEXT CHECK(signal IN ('up','down')), note TEXT, created_at)`; `post_mortems` gains
   `user_signal TEXT NULL` and `outcome_source TEXT NULL` (`llm` | `next_turn` | `user`).
   Backfill `outcome_source='llm'` for existing rows.
3. **API contract (W3 depends on it):**
   - `POST /api/sessions/{sid}/messages/{mid}/feedback` body `{"signal": "up"|"down"|null,
     "note": string?}` → upsert; `null` deletes. Returns `{message_id, signal, note}`.
   - `GET /api/sessions/{sid}/feedback` → `{items: [{message_id, signal, note, created_at}]}`.
   - `GET /api/trust` → `{grader: {agreement, n, holdout: <snooze_state trust.grader_holdout
     or null>}, outcomes: {by_source: {llm, next_turn, user}, graded_7d, user_turns_7d},
     entries: {by_status: {...}, unfounded: n}, canaries: {contaminated_14d: n, runs_14d: n,
     fails_14d: n}, trials: []}`. Missing inputs return zeros, never 500.
4. On feedback write: set `post_mortems.user_signal` for the turn's latest attempt, set
   `outcome_source='user'`, and apply corrective signal deltas for the entries in that
   post-mortem's `used_hints`/`cited_policies` via new `core/feedback.py` (do not edit
   `attribute()`; W1 owns it).
5. `core/metrics.py`: `grader_agreement()` = share of post-mortems with a user signal where
   verdict agrees (pass↔up, non-pass↔down).

### W3 Feedback UI (owner: static/**, tools/ui-gate)

Thumbs up/down on assistant messages: desktop hover actions and the touch action sheet,
calling the W2 contract; optimistic state; a11y labels; optional note prompt on thumbs-down
via the existing sheet/confirm components. A "Trust" tab in the Adaptive modal that renders
whatever `GET /api/trust` returns and degrades gracefully on 404. Update docs/in-app labels
inventory if the gate checks it. Run tools/ui-gate.

### W4 Measurement statistics, receipts, grader hold-out (owner: core/adaptive/tripwire.py, core/adaptive/engine.py validate_edit, core/adaptive/lint.py, core/adaptive/retire.py, core/dream/promote.py + core/refine.py + core/telos/evaluate.py receipt stamping, new core/adaptive/receipts.py, new data/eval/grader/, new core/reflect_holdout.py or snooze activity)

1. Tripwire post-mortem drift: replace the 20-turn ratio with a two-proportion z-test.
   Baseline = up to 100 graded turns before apply (min 30), post = graded turns after apply
   (min 30). Outcome per turn = `user_signal` if present else verdict. p<0.05 flags
   `suspect`; p<0.01 with `adaptive_auto_rollback` and new flag `adaptive_pm_drift_rollback`
   (default False) rolls the batch back through the existing journal path and notifies.
2. Receipts: `core/adaptive/receipts.py` parses evidence strings of the form `pm:<id>`,
   `candor:<key>`, `signal:<type>/<subject>`, `feedback:<message_id>`, `hypothesis:<id>`,
   `session:<id>` and resolves them against the DB. An entry is `grounded` when at least one
   ref resolves to a recorded outcome (pm, candor, signal, feedback, or a hypothesis whose
   evidence contains one of those); otherwise `unfounded`. Computed from the create event's
   evidence, no migration. Unfounded entries never auto-approve (they wait for a human);
   grounded ones keep the veto window. Producers stamp refs: refine (`pm:` ids it read),
   dream (`hypothesis:` + the hypothesis's `pm:`/`candor:` refs), telos (`hypothesis:` ids).
3. Grader hold-out set: `data/eval/grader/*.json`, 8 to 10 fixtures (user request,
   transcript excerpt, final response, expected verdict, expected failure_cause), covering
   clean pass, phantom deliverable, refusal-as-completion, correct escalate, over-strict
   trap. A nightly step runs the reflect grader on each with the live model and writes
   `{accuracy, n, by_case, ran_at, model}` to snooze_state key `trust.grader_holdout`.
   Fixtures must never be written to memory or the workspace.

### W5 Canary contamination, generated sentinels, skill rollback (owner: core/canary/**, core/skills/proposals.py, api/routers/skills.py, api/routers/canary.py, core/memory/** and core/refine.py and core/scout/** ONLY for canary exclusions, new data/canaries/gen-*/)

1. Audit and close every canary→learning leak: distill of canary sessions, refine over
   canary sessions, scout memory preload / deep_recall inside canary sessions (disable
   memory recall for `session_type == "canary"`), memory writes from canary sessions (verify
   the allowlist), space suggestions / titles. Each closed leak gets a test.
2. Generated fixtures: a canary directory may carry `generate.py` exposing
   `generate(seed: int) -> {"prompt": str, "files": {path: str}, "gates": [...]}`. The
   runner calls it with a fresh random seed per run, records the seed in
   `gate_results_json`, and never persists the expected values anywhere the agent can read.
   Add three: `gen-file-create`, `gen-grep-count`, `gen-json-transform` (tags
   `[sentinel, generated, holdout]`). DO NOT edit existing `data/canaries/*/CANARY.md`
   (the box has local edits in those tracked files; a pull would refuse).
3. Post-run contamination scan: if a canary session called any memory tool, read any file
   outside its temp workspace, or its transcript names another canary, set
   `canary_runs.outcome = "contaminated"`, exclude the run from tripwire testimony and
   baselines, and notify.
4. Holdout rule: `holdout`-tagged canaries are never proposed from, never referenced in
   refine/dream prompts, and their session transcripts are excluded like all canaries.
5. Skill rollback: `restore_skill_backup(proposal_id)` in core/skills/proposals.py,
   `POST /api/skills/proposals/{id}/rollback`; a `verify:` canary failure within 7 days of an
   auto-apply restores the backup when `skill_proposal_auto_rollback` (default False) and
   notifies.

## Batch 2 (after batch 1 merges): W6 trial arms

- New entry status `trial`. With `adaptive_trial_enabled` (default False), auto-applied
  `policy`/`prompt_note`/`routing_hint` entries enter `trial` instead of `active`.
- Per turn, the session computes one `turn_key = f"{session_id}:{turn_id}"`; render.py
  renders a trial entry iff `sha1(turn_key + entry_id)[0] % 2 == 0`, identically for the
  scout prompt and the compiled system prompt.
- Reflect's post-mortem payload records `rendered_entries` and `held_out_entries`.
- Sweep (`core/adaptive/trial.py`, snooze activity): per trial entry, treated vs control
  outcomes from post-mortems since the entry's creation (outcome precedence as above),
  two-proportion test. Promote to `active` when n≥40 per arm and not significantly worse, or
  early when p<0.05 better; retire when p<0.01 worse with n≥40 per arm; after 28 days
  inconclusive → `active` tagged `unproven` in the event evidence. Journal every decision
  with the counts and p-value. `/api/trust.trials` lists them; the Trust tab shows them.

## Deploy

Batch 1 → check.sh green → push → box `git pull && docker compose up -d --build` → health →
grep the container for new code → set box flags (`adaptive_pm_drift_rollback`,
`skill_proposal_auto_rollback`, later `adaptive_trial_enabled`) → delete the live
"ask_user degraded" routing hint → smoke the feedback API and /api/trust → watch the
deploy canary sweep. Batch 2 → same again.

## Acceptance (what "done" looks like on the box)

- Every real turn gets a grade; `outcome_source` mix is visible; thumbs land in post-mortems.
- A cited policy can accrue failures; `scout_signals` no longer shows 0 failures everywhere.
- The ask_user hint is gone and cannot be re-minted.
- Generated sentinels pass on a clean run and the contamination scan stays at zero.
- Tripwire drift decisions carry n and p; no more 20-turn ratios.
- Unfounded entries are counted and held; grounded ones flow.
- Trial arms report treated/control counts per entry.
