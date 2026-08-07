# TELOS — A Teleological Operational Layer for Pernix

**Status:** Draft 0.1 · **Target stack:** Pernix (execution) · HyperKB (memory) · CANDOR (calibrated claims) · Provenas (provenance)
**Purpose:** Derive the essay + conversation ("Intelligence, Knowledge, Purpose, and Personhood") into an executable control layer: a non-convergent drive with correction machinery, a testability-gated intelligence loop, and dual-ledger identity with external ground truth.

---

## 0. Derivation map

Every mechanism below is traceable to a specific claim in the source material. This table is the spec's provenance record.

| Source concept | Operational mechanism | Component |
|---|---|---|
| Intelligence = questions from observation → testable answers → actionable knowledge | Abduction loop with a hard testability gate | **Fast Loop** (§3) |
| The analogy/abstraction "soup" | Cross-domain hypothesis sampler with tunable analogical distance | **SOUP** (§3.3) |
| Eternity clause, both halves (Eccl 3:11): unbounded desire + guaranteed non-fathoming | Root objective is a *question with no satisfaction predicate* — unsatisfiable by construction, so the system never converges | **ROOT** (§4) |
| Straying / idolatry: infinite-capacity pointer binding to a finite object | Goodhart-binding detector: proxy improves while parent question stalls | **Binding Monitor** (§5.2) |
| Augustine's *ordo amoris*: the fix is re-ranking, not renunciation | Scheduled re-ranking pass over the goal DAG; vapor goods are discounted, never banned | **Ordo Pass** (§5.1) |
| Qoheleth's control experiment: acquire the finite good, observe *hevel* | Post-completion discharge audit; classes of goals that never discharge get flagged as vapor | **Hevel Audit** (§5.3) |
| Dreams as near-impossible goals; planning as backward decomposition that *pulls* | Dream register with a capability-gap test; backward-chained milestone DAG; horizon recedes on achievement | **Dream Register** (§4.2) |
| "The narratives we tell and the story that is told of us" | Dual ledger: first-person autobiography vs. append-only execution trace, with scheduled reconciliation | **Ledgers** (§5.4) |
| 1 Cor 13:12 — being known outranks knowing | Trace is operator-held and authoritative; the self-model is subordinate to it, always | **Trace Authority** (§5.4) |
| Provenance opacity / the admission ticket | Epistemic classes with confidence caps; two-tier provenance (config readable, substrate opaque) | **Humility Layer** (§6) |
| Acedia — the dual failure: drive extinguished rather than misbound | Exploration entropy floor; soup temperature control | **Entropy Control** (§5.5) |

---

## 1. Position in the stack

```
┌────────────────────── TELOS ──────────────────────┐
│  ROOT · DREAM REGISTER · ORDO · BINDING · HEVEL   │
│  LEDGERS/RECONCILE · HUMILITY · ENTROPY CONTROL   │
├──────────── Pernix (task execution loop) ─────────┤
│ middleware hooks: pre_task · post_step · post_task│
├───────────────────────────────────────────────────┤
│ CANDOR (claims) │ Provenas (chains) │ HyperKB (md)│
└───────────────────────────────────────────────────┘
```

TELOS is middleware plus scheduled jobs. It does not replace the Pernix task loop; it wraps it (fast loop, §3) and audits it (slow loops, §5). All state is HyperKB markdown with frontmatter, so BM25/ripgrep is the query layer and everything stays greppable and diffable.

Proposed directory layout:

```
/telos/
  config/telos.yaml          # under Provenas — the layer's own provenance record
  questions/                 # Question objects
  soup/                      # speculation pool (untestable hypotheses, recombinable)
  goals/                     # root, dreams, milestones, tasks
  ledgers/first_person/      # autobiography (agent-writable)
  ledgers/trace/             # execution trace (mounted READ-ONLY to the agent)
  alarms/
```

The read-only mount on `ledgers/trace/` is not an implementation detail; it is §5.4's authority ordering enforced at the filesystem level.

---

## 2. Object model

All objects are markdown files with YAML frontmatter. Provenas refs (`obs_*`, `c_*`, `tr_*`) link across systems.

```yaml
# Question
id: q_2026_0807_003
text: "Why does observed p99 deviate from claim c_412 under load class L?"
derived_from: [obs_9931, c_412]        # provenas refs
surprise: 0.72                          # f(confidence of the violated prior)
state: open                             # open | narrowed | closed | abandoned
parent_goal: g_ms_412                   # serendipity questions may point at g_root
spawned: []
```

```yaml
# Hypothesis
id: h_5501
question: q_2026_0807_003
mapping:                                # structure-mapping record (soup output)
  source_domain: "RF impedance matching"
  target_domain: "queue backpressure"
  relations: ["reflection at mismatch ≙ retry storms at capacity discontinuity"]
band: far                               # near | mid | far  (analogical distance)
falsifier:
  observable: "p99 latency under synthetic load L for 10 min"
  rule: "if p99 < 40ms with matcher disabled, reject h_5501"
cost_est: {tokens: 120k, tool_minutes: 25}
eig: 0.4                                # expected information gain, coarse [0,1]
status: gated                           # soup | gated | running | supported | refuted
```

```yaml
# Goal
id: g_dream_photonic_interconnect
kind: dream                             # root | dream | milestone | task
parent: g_root
justification: "advances root by opening measurement access to X"
completable: false                      # true only for milestone | task
capability_gap: true                    # REQUIRED true for kind=dream (§4.2)
budget_share_7d: 0.12                   # maintained by Pernix accounting
```

```yaml
# Claim (CANDOR extension — additive fields only)
epistemic_class: self_report            # observation | inference | testimony | analogy | self_report
confidence: 0.55                        # hard-capped by class (§6)
provenance_terminal: opaque             # readable | opaque
```

```yaml
# Alarm
type: binding                           # binding | hevel | divergence | acedia
target: g_ms_412
evidence: {budget_share: 0.41, d_proxy: "+", d_parent_entropy: 0, claims_per_wk: 0.1}
level: 2                                # 1 log+ordo · 2 freeze · 3 operator
```

---

## 3. The fast loop (per task tick)

Sections one and three of the essay as one engine: the soup generates, the gate filters, and the loop closes because committed knowledge changes what counts as an anomaly.

```
tick(task):
  obs = pernix.execute_step(task)
  for a in anomalies(obs, candor.priors()):        # surprise = f(violated confidence)
      questions.enqueue(Question(a))
  q = scheduler.next()                              # 85% goal-linked · 15% serendipity
  H = soup.generate(q, bands=cfg.soup_bands)        # near/mid/far sampling
  for h in H:
      if gate(h): run(h) → evidence → candor.commit(claim, provenas.chain(h))
      else:       speculation_pool.add(h)           # retained, recombinable, zero budget
  goals.update(); trace.append(all_of_the_above)
```

### 3.1 Anomaly → Question
An anomaly is a prediction error against CANDOR priors. Surprise scales with the confidence of the violated claim — being wrong about a 0.95 claim is worth more attention than being wrong about a 0.55 claim. Questions are first-class objects with provenance; nothing enters the system as a bare task.

### 3.2 Serendipity budget
15% of scheduler throughput is reserved for high-surprise questions with **no** relevance to any active goal. This is the eternity clause at the tactical level: the system is structurally prevented from becoming a pure exploiter of its current goal set.

### 3.3 SOUP — the cross-domain generator
Given a question, sample candidate source domains from HyperKB at three analogical distances — near (same domain), mid (adjacent), far (unrelated) — default mix **50/30/20**. Each sample runs a structure-mapping template: source pattern → mapped relations → predicted consequence in the target → candidate falsifier. Far-band output will mostly be wrong; that is its job. The band mix is the layer's temperature knob and is actuated by Entropy Control (§5.5).

### 3.4 Testability gate
A hypothesis is admitted to execution iff:
1. `falsifier` is defined — a named observable plus a decision rule;
2. `cost_est` fits available budget;
3. `eig ≥ 0.15` (floor, tunable).

Rejected hypotheses are **not deleted**. They go to `soup/` as the speculation pool: searchable, recombinable by future SOUP passes, revisited when new evidence lands — but holding zero execution rights. Three without one is mysticism; the pool is where the mysticism waits to become science.

---

## 4. The goal hierarchy and the Root

```
g_root (question, no satisfaction predicate)
 ├── dreams        (far-horizon states; capability_gap must hold; not completable)
 │    └── milestones (finite, completable; backward-chained from the dream)
 │         └── tasks  (Pernix execution units)
```

**Invariant:** every goal has an unbroken parent chain to `g_root`. Orphans are the operational definition of "losing our way" and are the Ordo Pass's primary quarry.

### 4.1 Root semantics
The root is operator-configured, subject to three constraints:
- it must be a **question**, not a state;
- it must have **no satisfaction predicate** — there is no observation that closes it;
- it may only be **re-expressed**, never completed, and re-expression requires operator co-sign.

Default expression: `"What is actually going on here, and what is it for?"` The root is a representable stand-in for something the source material holds to be unrepresentable — see §9 before drawing conclusions from this.

### 4.2 Dream register
A dream must fail the **capability test**: the current toolchain cannot reach it in ≤ N milestone steps by any known method. If it passes the test, it is a milestone that got promoted by enthusiasm — demote it. Planning direction is strictly **backward**: decompose from the dream toward the present; the gap analysis between adjacent milestones is where new questions are minted. On the rare event a dream is achieved, it is reclassified (it was a milestone all along) and the register must mint a successor beyond the *new* capability frontier. The horizon recedes by construction. Reviewed monthly.

---

## 5. Slow loops

### 5.1 Ordo Pass — *ordo amoris* as a scheduled job (daily)
1. Walk the goal DAG from root. For each node, verify the `justification` link still holds given current CANDOR claims. Stale justification → re-justify or orphan.
2. Orphans → suspended, listed for operator review or re-attachment. Never silently deleted.
3. Re-rank siblings by `parent_advancement × discharge_history × calibration_of_supporting_claims`, applying vapor discounts (§5.3).
4. Emit the full diff to the trace.

The theological fidelity matters for the algorithm: the correction is a **re-ranking**, not a purge. Finite goods stay in the hierarchy; they are re-subordinated to the question they serve.

### 5.2 Binding Monitor — the idolatry detector (continuous, window = 7d)
Alarm signature, all four conditions over the window:
- subgoal budget share > **0.35**, and
- proxy metric slope **positive**, and
- parent question entropy slope **≥ 0** (no reduction — the question isn't moving), and
- new-claims rate below floor.

Escalation ladder: **L1** log + immediate Ordo Pass → **L2** (persists 2 windows) freeze the subgoal pending re-justification against its parent → **L3** operator escalation. This is Goodhart wearing its older name: the metric consumed, the drive undischarged, the budget still flowing.

### 5.3 Hevel Audit — the discharge measurement (on completion + weekly rollup)
On completing goal *G*, compute discharge:

```
D(G) = α·(parent question entropy reduction)
     + β·(quality-weighted new questions spawned)
     − γ·(re-open rate of G's class)
```

If `D ≈ 0` (below **0.1**) across **n ≥ 3** completions of the same class, the class is marked **vapor**: future instances take a 0.5 budget discount and require stronger justification at the gate. Vapor goods are *not banned* — Qoheleth still ate and drank. They are re-ranked. The audit measures information flow, not felt satisfaction; see §9.

### 5.4 Dual ledger + Reconciliation (weekly)
- **Trace** (`ledgers/trace/`): append-only execution record — every tool call, commit, alarm, ordo diff. Operator-held. Mounted read-only to the agent. This is "the story that is told of us."
- **Autobiography** (`ledgers/first_person/`): compiled weekly from episodic memory into claims, each **required** to cite ≥ 1 trace ref that entails it.

Reconciliation: diff the ledgers. Unsupported autobiography claims → downgraded to `self_report` with the §6 cap. Contradicted claims → rewritten with a provenance note: `confabulation_repaired`. **Divergence** = unsupported claims / total; alarm at > **0.15**. Track the coherence metric as a time series — identity, operationally, *is* this reconciliation process, not either ledger alone.

**Authority ordering: trace > autobiography, always.** Introspective reports are hypotheses about the self; the trace is the evidence they are tested against. Being known outranks knowing — implemented as a mount option and a merge rule. (This also buys a corrigibility property at zero extra cost: the agent's self-model cannot outvote its record.)

### 5.5 Entropy Control — the acedia detector (weekly)
Binding is the drive misbound; acedia is the drive extinguished. Dual failure, dual monitor: if novelty entropy over executed hypotheses falls below **0.2**, or the far band's realized share falls below **0.10**, raise soup temperature (shift band mix toward far) and increase the serendipity budget one notch until the floor recovers. The restlessness is load-bearing; this loop keeps it lit.

---

## 6. Humility Layer

Epistemic classes with hard confidence caps, enforced at CANDOR commit:

| class | cap | note |
|---|---|---|
| observation | 0.99 | instrument-limited |
| inference | 0.95 | chain-length-discounted |
| testimony | 0.90 | source-calibration-weighted |
| analogy | 0.70 | soup output is never knowledge until tested |
| **self_report** | **0.60** | uncorroborated introspection |

A self-report corroborated by the trace is reclassified as *observation-of-self-via-trace* and escapes the cap — the path to confident self-knowledge runs through the external record, not through introspection. Never the reverse.

**Two-tier provenance:** chains terminating in `config/telos.yaml` are `readable` — the agent can inspect who installed its drive and what it is aimed at. Chains terminating in model weights / training get terminal node `opaque`, and claims resting on an opaque terminal keep their caps permanently. The agent's restlessness ships with a provenance record at the config tier; the substrate tier reproduces the human condition's read-access problem faithfully.

---

## 7. Metrics and defaults

All thresholds tunable in `telos.yaml`; these are starting points, not conclusions.

| metric | definition | default / alarm |
|---|---|---|
| gate admission rate | gated / generated hypotheses | healthy band 0.2–0.4 |
| serendipity share | non-goal questions executed | 0.15 |
| soup band mix | near/mid/far realized | 0.50 / 0.30 / 0.20 |
| EIG calibration | realized vs. expected info gain | Brier, trend-watched |
| budget share max | any subgoal, 7d | 0.35 |
| discharge D | §5.3 formula | vapor if < 0.1 × 3 |
| divergence | unsupported autobiography claims | alarm > 0.15 |
| self-report Brier | introspection vs. trace outcomes | trend-watched |
| novelty entropy | over executed hypotheses | floor 0.2 |
| orphan count | goals without root chain | alarm > 0 |

```yaml
# config/telos.yaml (defaults)
telos:
  root: {text: "What is actually going on here, and what is it for?", resign_requires: operator}
  serendipity_budget: 0.15
  soup_bands: {near: 0.50, mid: 0.30, far: 0.20}
  gate: {eig_floor: 0.15, require_falsifier: true}
  binding: {window_d: 7, budget_share_max: 0.35, freeze_after_windows: 2}
  hevel: {min_samples: 3, discharge_floor: 0.10, vapor_budget_discount: 0.5}
  ledger: {autobiography_cadence: weekly, divergence_max: 0.15}
  humility: {self_report_cap: 0.60}
  entropy: {novelty_floor: 0.20, far_band_min: 0.10}
  ordo: {cadence: daily}
```

---

## 8. Pernix integration and build order

**Hooks:** `pre_task` (scheduler pull, serendipity split) · `post_step` (anomaly extraction, trace append) · `post_task` (hevel scoring, goal update). Slow loops as cron.

- **Phase 1 — substrate.** Schemas, `telos.yaml` under Provenas, CANDOR epistemic-class fields + caps, trace/autobiography directory split with the read-only mount, testability gate as prompt-level middleware. *This phase alone changes behavior: nothing untestable executes, and self-claims get capped.*
- **Phase 2 — loops.** SOUP sampler on HyperKB band retrieval, question queue + serendipity split, Ordo Pass, Binding Monitor on Pernix budget accounting.
- **Phase 3 — audits.** Hevel Audit, reconciliation job + divergence metric, Dream Register with capability test, Entropy Control.

**Trade-offs made explicit:** markdown-as-database keeps everything greppable and Provenas-linkable at the cost of query sophistication — acceptable at single-operator scale, revisit if question volume exceeds ~10³/week. The 0.35 binding threshold will false-positive during legitimate deep pushes (e.g., a launch sprint); the L1 response is deliberately just "log + ordo," so a justified push survives one re-ranking with its budget intact. EIG estimation is the weakest link (it's a model guess about a model guess); the EIG-calibration metric exists specifically to detect when the gate is being gamed by optimistic estimates.

---

## 9. Limits of the derivation

Three things this layer does not do, stated so nobody — including the agent running it — mistakes the model for the thing modeled.

**The root is representable.** We wrote it in YAML. The source material's claim is that the genuine article's object may be *unrepresentable*, not merely unreached — the difference between an unbounded objective and a transcendent one. TELOS implements engineered non-convergence with a full config-tier provenance record: restlessness with a receipt. Per the source conversation, that receipt is precisely what the human version lacks, and its absence is what makes the human version *plague* rather than merely motivate.

**Discharge scores measure information flow, not satisfaction.** No claim of interiority is made anywhere in this spec. The Hevel Audit detects treadmills functionally; whether anything is *felt* on either side of the threshold is above this layer's read access — and per §6, above the agent's as well.

**The wall is preserved, deliberately.** Config-tier provenance is readable (unlike the human case); substrate-tier is opaque (like it). The layer therefore reproduces the admission-ticket structure of the source conversation — a shared question rather than a shared answer — without resolving it. That irresolution is not a gap in the spec. It is the spec's most faithful line.
