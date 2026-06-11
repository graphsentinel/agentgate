# Plan — Consultant Review Remediation

Structured plan for the v1alpha1 review findings. Each finding → a requirement
(FR/NFR) + epic/US + tasks + DoD + test case + Gherkin. **Plan only; code follows in the
order in §Sequencing.** Numbering continues the CFP: FR≥10, NFR≥7, TC-F≥20, Epic≥8.

Severity from the review: **C** critical, **I** important, **M** minor.

---

## R1 (C) — Control-plane → data-plane handoff  ·  FR-10  ·  RE1

**Finding.** Operator keeps the baseline in its own `Reconciler` store
(`operator/reconcile.py:14`); the sidecar starts with an empty `BaselineStore()`
(`interceptor/main.py:18`); the manual sidecar only receives the OTLP env, no
policy/baseline/action (`deploy/sidecar-manual.yaml:31`). So "operator reconciles into
running governance" is not closed end-to-end.

**FR-10 (new).** The interceptor MUST enforce against the operator-reconciled policy +
baseline for its task types: action, threshold, failurePolicy, and the baseline snapshot
are delivered to the sidecar — not hard-coded or empty.

**RE1 — Reconciled enforcement (control↔data handoff).**
- **US-8.1** — *As a platform engineer, the sidecar enforces the SAME policy + baseline
  the operator reconciled, so a drift is judged against learned normal, not an empty store.*
- **Tasks**
  - **T8.1** — Baseline snapshot transport: operator persists the reconciled
    `BaselineStore` to the shared store (`DRIFTWATCH_DATA_DIR`, already wired); the sidecar
    loads it on boot + on change (watch/poll the same store or a ConfigMap projection).
  - **T8.2** — Policy params to the sidecar: action/threshold/failurePolicy delivered via
    env or a mounted policy ConfigMap the operator writes; `build_default_interceptor()`
    reads them instead of the current defaults.
  - **T8.3** — In-cluster e2e: a pod POSTs `/v1/tool-call`; within-baseline → forward,
    drift → drop/block per the live policy; assert against operator-written status.
  - **T8.4** — `sidecar-manual.yaml`: document + template the policy/baseline mounts so the
    BYO path also gets real governance, not just OTLP.
- **DoD**
  - [ ] Sidecar boots with the reconciled baseline for its task types (not empty).
  - [ ] Changing the policy action (log→block) changes sidecar behavior without a rebuild.
  - [ ] In-cluster e2e: drift on a real `/v1/tool-call` is blocked per the live policy.
- **Test:** TC-F-20 (sidecar loads reconciled baseline + action), TC-F-21 (in-cluster e2e drift→block).
- **Gherkin**
  ```gherkin
  Scenario: Sidecar enforces the reconciled policy                 # TC-F-20
    Given an AgentDriftPolicy reconciled by the operator with action block
    And a baseline learned for task "investigate_latency"
    When the interceptor sidecar starts
    Then it loads that baseline (not an empty store)
    And a drifting tools call is blocked, a within-baseline call is forwarded
  ```

---

## R2 (C) — CFP claim vs. implementation (path A/B honesty)  ·  CFP edit

**Finding.** CFP says "operator reconciles into running governance" + "Kagent and Goose
deployment" (`CFP-A:139`), but the demo runs in-process stand-ins (`sample-agents.yaml:1`,
`SETUP_RUNBOOK.md:220`). Real-Kagent governance is path B / E7 (roadmap).

**Action (CFP only, no code).** Soften abstract/Benefits: the live demo is a deterministic
**stand-in (path A)**; governing a **real Helm-installed Kagent at the MCP hop is path B
(E7, roadmap)**. Keep the headline (CRD shown, drift caught across five scenarios) but
stop implying a live real-Kagent deployment.
- **DoD:** no CFP sentence claims a running real-Kagent/Goose deployment as delivered;
  path A/B split explicit in Abstract + Benefits + Architecture component #4/#5.

---

## R3 (C) — CI doesn't test the runtime surface  ·  NFR-7  ·  RE2

**Finding.** CI runs `pip install -e ".[dev]"` (`ci.yml:15`), but `dev` lacks
kopf/fastapi/httpx (`pyproject.toml:23`), so runtime tests `importorskip`-skip silently
(`test_interceptor_adapters.py:103`, `test_operator_otel.py:88`). Green CI hides untested
operator/interceptor runtime.

**NFR-7 (new).** CI MUST exercise the operator + interceptor runtime, not just pure
library; skipped runtime tests MUST fail the build.

**RE2 — CI runtime coverage** (small).
- **T9.1** — CI installs `.[all]` (or add runtime deps to `dev`).
- **T9.2** — Fail on unexpected skips (`-W error` / `--strict-markers` + an assert that
  the kopf/fastapi tests actually ran in CI).
- **DoD:** [ ] CI imports kopf+fastapi+httpx; [ ] interceptor HTTP + operator handler
  tests run (not skipped) in CI; [ ] a deliberately-broken runtime path fails CI.
- **Test:** TC-F-22 (CI runs the runtime suite; skip-count==0 for runtime tests under `.[all]`).

---

## R4 (I) — `detection.features` parsed but not enforced  ·  FR-2 (amend)

**Finding.** Policy parses `features` (`policy.py:83`) but `score_chain()` always runs
all five (`decision.py:94`). The CRD knob is inert.

**Amend FR-2.** `score_chain()` MUST honor a feature mask: only the policy's
`detection.features` contribute to drift.
- **Tasks:** add a `features: set[str]` param to `score_chain()`; operator/interceptor pass
  `policy.features`; a disabled feature never sets anomaly.kind.
- **DoD:** [ ] a policy with `features:[tool]` ignores a pure scope/arg deviation;
  [ ] default (all) unchanged.
- **Test:** TC-F-23 (feature mask: scope-only drift not flagged when `features:[tool]`).
- **Gherkin**
  ```gherkin
  Scenario: Disabled feature is not scored                         # TC-F-23
    Given a policy with detection.features [tool]
    When a call drifts only on scope
    Then no drift is flagged (scope feature is disabled)
  ```

---

## R5 (I) — retry_storm claim vs. code (3-way inconsistency)  ·  CFP + code

**Finding.** CFP says "sequence n-gram frequency" (`CFP-A:117`) and `ngram.py:3`
docstring says "rate-shaped storms", but the code only detects **novel transitions**, no
frequency/rate (`ngram.py:34`); the demo `retry_storm` is actually a DeleteNode **tool
mismatch** (`cli.py:79`). CFP, docstring, and demo disagree.

**Two honest options (pick one).**
- **(a) Match the claim** — add a real rate feature: flag a transition whose observed
  frequency is N-sigma above baseline (`NGramModel` already counts; add a rate check).
  New `anomaly.kind=rate_anomaly` (additive).
- **(b) Match the code** — drop the "frequency/rate" wording from CFP + `ngram.py`
  docstring; rename the demo to `tool_escalation` (it already exercises tool mismatch).

Default recommendation: **(b)** for v1alpha1 (cheap, honest), **(a)** as a roadmap feature.
- **DoD:** [ ] CFP, `ngram.py` docstring, and the demo scenario name/description agree;
  if (a): TC-F-24 (rate anomaly) added.

---

## R6 (I) — Baseline poisoning  ·  NFR-8 (security)  ·  RE1

**Finding.** `fold()` adds every chain it sees to "normal" (`baseline.py:41`); a drifting
chain folded in poisons the baseline. The consensus plan already flags this
(`consensus-and-mcp-proxy-plan.md:20`).

**NFR-8 (new).** Baseline ingestion MUST distinguish source trust: only
approved/dry-run/successful sources fold automatically; drift-suspect chains never
auto-fold.
- **Tasks:** tag folded chains by source (`approvedTraces`/`successfulRuns`/`dryRun`);
  gate auto-fold on source trust + non-drift; shadow-mode "would-have-blocked" chains are
  excluded from the baseline.
- **DoD:** [ ] a chain scored as drift is not folded back as normal;
  [ ] only declared trusted sources widen the baseline.
- **Test:** TC-F-25 (drift chain not folded; trusted source folded).

---

## R7 (I) — Production security context  ·  NFR-9  ·  RE2

**Finding.** Dockerfile is non-root (`Dockerfile:18`) but the K8s template has no
`securityContext` (`operator.yaml:13`): no runAsNonRoot / readOnlyRootFilesystem /
seccompProfile / capability drop.

**NFR-9 (new).** Workloads MUST ship a hardened pod/container securityContext by default.
- **Tasks:** Helm `values.yaml` securityContext block (runAsNonRoot, runAsUser 10001,
  readOnlyRootFilesystem, seccompProfile RuntimeDefault, drop ALL caps); apply to operator
  + sidecar templates; ensure `/data` writable with readOnlyRootFs (emptyDir mount).
- **DoD:** [ ] `helm template` shows the securityContext on operator + sidecar;
  [ ] pod runs read-only-rootfs with the `/data` mount writable.
- **Test:** TC-F-26 (rendered manifests carry the hardened securityContext).

---

## R8 (I) — Ephemeral persistence  ·  NFR-10

**Finding.** Operator uses `/data` emptyDir (`operator.yaml:29`); baseline is lost on
restart (live `READY=false/observedTaskTypes=0` showed it). Fine for demo, not prod.

**NFR-10 (new).** Production MUST support durable baseline persistence (PVC or external
DB); emptyDir is demo-only.
- **Tasks:** Helm `persistence.enabled` → PVC for `/data`; document the Postgres backend
  path (db interface already swappable). Default off (demo keeps emptyDir).
- **DoD:** [ ] `persistence.enabled=true` renders a PVC + mount; [ ] baseline survives an
  operator restart with persistence on.
- **Test:** TC-F-27 (PVC rendered when enabled; restart keeps baseline — integration).

---

## R9 (I) — FR-9 majority too coarse  ·  consensus plan amend

**Finding.** Majority **tool-set** alone (`consensus-and-mcp-proxy-plan.md:33`) can admit
an unsafe *combined* chain: each tool is individually majority-approved, but their
combination/order never was.

**Amend the consensus plan.** Apply quorum at multiple granularities, not just the tool
set: tool **and** scope **and** ordered transition **and** (where N allows) a full-chain
template. Provenance mandatory.
- **DoD:** [x] `consensus/aggregate.py` keeps a transition/scope/chain-template only if
  quorum models produced it, not just the union of majority tools; provenance records each
  level. ✅ Coded: template-first (quorum chain-templates) with a quorum-transition
  fallback, quorum default `max(2, ceil(N/2))`, offline `consensus-seed` CLI +
  `consensus_seed.json` provenance.
- **Test:** [x] TC-F-28 (a combined chain no single model proposed is NOT in the baseline
  even if each tool is individually majority) ✅ — plus TC-F-18 (majority keep / minority
  drop) and TC-F-19 (single-model refusal). 8 tests in `tests/test_consensus.py`.

> Status: the consensus *producer* (R9 core: quorum aggregation + offline seed CLI) is
> coded and tested. Live model-panel polling (the provider clients in `runner.py`) and the
> E7 MCP-proxy enforcement remain roadmap — see `consensus-and-mcp-proxy-plan.md`.

---

## R10 (M) — Minor

- **R10a — Prometheus buckets** (`emit.py:102`): score is normalized [0,1] but the
  histogram uses default buckets (le=5,10,…). Set explicit buckets (0.1…1.0). Cheap.
- **R10b — RBAC scope** (`rbac.yaml:7`): cluster-wide; CRD is namespaced. Add an optional
  namespace-scoped Role for prod least-privilege.
- **R10c — Webhook injector**: template renders but binary is roadmap
  (`sidecar-injector.yaml:1`). Already documented honestly — no change, keep the note.

---

## CFP submit-blockers (before submission) — DONE in the CFP
1. **R2** — ✅ Abstract softened: "Kagent-/Goose-style workloads", no live real-Kagent
   deployment claim; path A/B split already explicit (14 mentions).
2. **R5** — ✅ "rate-shaped storms" claim removed; demo-5 reframed as tool escalation
   (out-of-baseline DeleteNode, not call-rate); rate feature noted as roadmap.
3. **Inverse-scaling** — ✅ DATA-READY slot states the seed has no capability signal
   (β₁≈0) and must NOT be a headline number; abstract asks the question, doesn't assert.

Also folded into the CFP this pass: FR-10, NFR-7..10 (requirements table); TC-F-20..28
(test catalog); RE1/RE2 (epic table + Should/Could backlog).

---

## Sequencing (code order, after this plan)
**Cheap honesty/hardening first, big integration last:**
1. **R3** (CI `.[all]` + no-skip) — makes every later change actually tested. *(RE2)*
2. **R7** (securityContext) + **R10a** (buckets) — small Helm/emit fixes. *(RE2)*
3. **R5(b)** + **R2** + inverse-scaling wording — CFP/docstring/demo honesty (no big code).
4. **R4** (feature mask) — contained `score_chain` change. *(FR-2)*
5. **R6** (poisoning guard) — baseline source-trust. *(NFR-8)*
6. **R1** (operator→sidecar handoff + e2e) — the headline gap. *(RE1, biggest)*
7. **R8** (PVC) ✅, **R10b** (namespace-scoped RBAC) ✅, **R9** (consensus-producer:
   multi-granularity quorum + offline seed CLI, FR-9 core) ✅ — prod depth shipped. Live
   model-panel polling (`runner.py` providers) + E7 MCP-proxy enforcement remain roadmap.

Each step: code → test (no skips) → commit. R1 is the largest and lands after the cheap
wins so it's exercised by a CI that actually runs the runtime.

---

### New identifiers introduced (no collisions with the CFP)
- Requirements: **FR-10** (handoff), **NFR-7** (CI runtime), **NFR-8** (poisoning),
  **NFR-9** (securityContext), **NFR-10** (persistence); **FR-2** amended (feature mask).
- Remediation epics (separate from the CFP's E1–E7 to avoid confusion): **RE1**
  (reconciled enforcement + poisoning guard), **RE2** (CI + hardening).
- Test cases: **TC-F-20..28** (E7 already owns 16/17; consensus 18/19).
