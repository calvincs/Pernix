# Surface prune — October 2026

Status: in progress (2026-10-02). Branch: `next-3.2-testing`.

## Why

A usage audit of the live box (30 days to 2026-10-02) ranked every feature by
real use against its cost. The core that earns its keep is chat, bash/files,
web tools, the Python REPL and memory. Several self-improvement systems run
every day without measurable benefit:

| Feature | Box evidence (30 d) |
|---|---|
| Canaries | 359 sessions vs 97 human sessions; ~23% of recorded tokens; 92% pass; 33 "contaminated" runs, almost all false positives |
| Adaptive layer | 644 proposals, 29 trials, none significant, most never treated (0/0) |
| Telos | 0 tool calls, 1 goal |
| Candor | 0 tool calls; feeds scout/dream invisibly with no shown benefit |
| Skill self-healing | 52 edits auto-applied, 1 trial use; 9 of 12 edited skills now exceed the 5,000-char injection cap |
| Scout | adds p50 29.7 s to a normal turn; time is in rounds 2-6 (1 round p50 8.9 s) |
| Planning, heartbeats, toolmaker/skillmaker, workflows, artifacts | 0-8 calls ever |

Usage is not the same as value, and activity is not benefit. Where a feature
cannot show an improved outcome it is retired; where it carries a real
workflow it is cut down rather than removed.

## Decisions

1. **Remove** workflows leftovers, the artifacts stats entry, the planning
   extension, heartbeats. Tables stay; no DROP.
2. **Remove** the feature-eval loop with planning (`evaluate` tool, `eval_auto`
   hook, `_AUTO_EVAL_BLOCK`): it only reads `data/registry.json`, which only
   planning wrote. The eval-retry turn plumbing in the state machine is a later,
   separate change.
3. **Remove** toolmaker and skillmaker creation tools. `install_package` moves to
   a small `packages` extension. A `custom_*` loader guard skips legacy files.
   Skills are authored as `data/skills/<name>/SKILL.md` files (humans: Skills
   editor; agent: bash). `allowed_write_roots()` is not widened.
4. **Retire Telos** fully.
5. **Retire Candor**: disable, then cut every integration and drop the vendored
   wheel. Ordinary per-tool success/failure logging stays (tool-message
   metadata, `post_mortems.tool_summary`, `scout_signals` type `tool`).
6. **Retire the adaptive layer** (entries, proposals, trials, tripwire, lint,
   Learning tab, adaptive tools, `/api/adaptive`). Tables and rows stay as
   history. Active entries are exported to `data/adaptive/ACTIVE-AT-RETIREMENT.md`
   by `scripts/export_adaptive_entries.py` (raw sqlite, no `core.adaptive`).
   Dream keeps its memory-hygiene effect: validated contradiction /
   memory_stale findings call `apply_memory_correction` directly;
   tool_pattern / lesson_ineffective become terminal report-only.
   `review.pending` moves to `core/skills/review.py` and counts skill
   proposals only.
7. **Canaries: change-driven, small, relevant.** No nightly heartbeat, no
   post_batch, no self-growth, no auto-maintenance, no `canary_run` agent tool.
   Triggers: deploy (debounced), model swap, manual run / Run all. Contamination
   is recorded, not alarmed, and its path heuristic is narrowed. Suite:
   gen-file-create, gen-json-transform, plus new `link-digest` (served local
   fixture, web read tools) and `youtube-captions-digest` (local VTT fixture,
   youtube-whisper skill). Memory round-trip is covered by a plain pytest, not
   a canary. Workers and `search_web` are left to unit tests.
8. **Skill self-healing becomes suggestions only.** Remove auto-apply, automatic
   rollback and stuck-mode trial hints; add a 30-day archive for stale
   proposals. Loading, manual apply, backups and manual rollback stay.
9. **Scout: one round.** New `scout_max_rounds` (default 1). With one round the
   existing last-round path offers only `submit_report`. Add `fallback_reason`
   to the scout event. Deleting the multi-round machinery waits for two weeks of
   data (follow-up S3).

## Stages

Two lanes, each in its own worktree, merged at the end.

**Lane A (sequential, one agent per stage):**

- **A1 Telos + Candor.** Create `static/js/components/modals/tab-kit.js` with all
  six shared helpers (`tabGlossary`, `makeDisclosure`, `resultLine` from
  telos.js; `actionBtn`, `setActionNotice`, `takeActionNotice` from
  adaptive.js). Fix `adaptive/receipts.py` candor read, `conftest.py`
  `telos_dir`, `sw.js`, Dockerfile `COPY vendor/`.
- **A2 Adaptive.** Export script first. Cut consumers, then producers, then the
  UI, then the API and tools, then the package, then config, then db accessors.
  Optional data-only migration v43 resolves open `adaptive.*` bell rows.
- **A3 Canaries.** As decision 7.
- **A4 Skills + Scout.** As decisions 8 and 9.

**Lane B (parallel with lane A):**

- **B1 Dead code.** As decisions 1-3.

Each commit carries its own code, tests and docs, in conventional-commit style.
`CHANGELOG.md` gets one `## Unreleased` bullet per retired feature, and
`docs/upgrade.md` gets a "What's gone in 3.2" section.

## Verification

Local:
- `./check.sh` green, apart from the known `killed_job` exit-code env failure.
- `tools/ui-gate` green, with its baseline refreshed where tabs changed.

Live on the box after deploy:
- The tool list has none of the removed tools. The Learning and Goals tabs are gone.
- Run all executes exactly 4 canaries.
- A smoke turn shows `scout_rounds = 1`.
- New skill proposals stay `pending`.
- `/api/health` is clean, `--dangerous` is still in Args, and there are no tracebacks.

Watch over two weeks, comparing against the pre-deploy baselines:

| Metric | Baseline | Target |
|---|---|---|
| Normal scout p50 | 29.7 s | ≤ 13 s |
| Normal reflect pass rate | 73.6% | Drop of no more than 8 points |
| Skill-injection rate | 19.9% | Holds |
| Canary token share | ~23% | Below 5% |

## Box rollout

1. Before any deploy, stop the spend: through the settings API set
   `canary_enabled`, `telos_enabled` and `candor_enabled` to false.
2. Back up. Check that no session is busy. Run `git checkout -- data/canaries`
   (the seeds carry local `parked: true` edits). Then
   `git pull --ff-only` and `docker compose up -d --build pernix`.
3. Run `scripts/export_adaptive_entries.py`. Move `data/telos` and `data/candor`
   to `data/backups/retired-2026-10/`.
4. Retire the untracked proposed canaries and `skill--*` through
   `DELETE /api/canary/{name}`. Remove the stray `retired.json` markers.
5. Settings:
   - `scout_timeout` 300 → 60
   - `scout_preload_memory_char_limit` 150 → 400
   - `canary_enabled` → true (manual and on-change runs only)
6. Optional, for the owner: revert bloated skills from their first backups in
   `data/skill_backups/`.

## Follow-ups (not in this change)

- **S3:** after two weeks at one round, delete scout's multi-round tools,
  revision loop and post-mortem search (about 550 LOC).
- **S2:** carry the prior scout report forward on short follow-up turns.
- **C2:** retire the eval-retry turn plumbing.
- Decide whether dream should keep generating tool_pattern / lesson_ineffective
  findings now that they are report-only.
- Drop skill-proposal generation entirely if nobody applies one by hand in 30 days.
