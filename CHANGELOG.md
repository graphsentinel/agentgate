# Changelog

Sprint-by-sprint delivery history (see the CFP's Implementation Plan, S0–S6).
Each entry corresponds to one sprint commit.

## S0 — Scaffold
- Repo skeleton matching the CFP Repository Layout (`src/driftwatch/{library,sdk,adapters,db,graph,otel,operator,interceptor,crd}`, `config/`, `deploy/`, `evaluation/`, `examples/k3d-cluster-demo/`, `tests/`).
- `pyproject.toml` (package `driftwatch` v0.1.0a0, optional extras: operator/interceptor/graph/dev).
- `AgentDriftPolicy` CRD manifest (`deploy/crd/`) with OpenAPI v3 validation; `status` is a subresource (operator-written).
- Sample policies: `kagent-cluster-ops` (enforce) + `shadow-mode` (log) under `config/policies/`. *(later removed — the single policy set lives in `examples/k3d-cluster-demo/manifests/`; `config/` keeps only `otel-targets.yaml`.)*
- `Makefile` (install/test/eval/cluster-up/obs-up/demo-1..5), `.gitignore`, CI workflow, README.

## S1 — Detection library
- `sdk/observation.py`: stable contract — `ToolCall` (with `arg_schema_hash` over key+type shape), `DecisionChain`, `RuntimeAdapter` base + registry (FR-8 foundation).
- `library/`: pure-statistics detection core shared by operator + interceptor —
  - `fingerprint.py` (tool, scope, argSchemaHash + category/risk),
  - `ngram.py` (tool-transition frequency + novel-transition detection),
  - `zscore.py` (streaming raw z-score + [0,1] normalized squash — the two-value model),
  - `baseline.py` (`TaskBaseline` + `BaselineStore`, rolling window, cold-start `ready` guard),
  - `decision.py` (four-feature scoring → anomaly.kind → gate.action; sequence drift attributed to the destination tool; vocabulary incl. additive `arg_schema_novel`),
  - `scaling.py` (OLS inverse-scaling, β₁>0 ⇒ bigger models drift more).
- `db/`: swappable persistence — `MemoryBackend` (dev/kind) + `SqliteBackend` (default, JSON-snapshot roundtrip).
- `tests/test_library_detection.py`: 9 green — TC-D-01/02/04/06, TC-F-04/13, z-score monotonicity, status-ready, sqlite roundtrip.
- Detection core adapted from Obs-Summit `gen_ai_otel/observation` (types/baseline/analyzer/scaling), generalized to the four-feature model.

Maps to: Implementation Plan S1; FR-2/FR-3; NFR-2; TC-D-*, TC-F-03/04/13.

## S2 — OTel emission + operator (Kopf)
- `otel/attributes.py`: gen_ai.agent.* / gen_ai.evaluation.* constants, verbatim from the Obs-Summit semconv. No `drift.*` namespace (C1). Additive: `gen_ai.agent.gate.action`, `computed.anomaly.kind=arg_schema_novel`.
- `otel/emit.py`: `build_span_attributes` (identity/baseline/gate on the span) + `build_evaluation_event` (score on the event); `Emitter` pushes OTLP or no-ops if OTel isn't installed (graceful).
- `operator/policy.py`: cluster-free `validate()` (TC-F-01) + `Policy`; `model_seed` surfaces an optional `models:` source (FR-9).
- `operator/reconcile.py`: `Reconciler` builds a live `BaselineStore` (memory/sqlite), folds runs, computes `status` (`baselineReady`, `observedTaskTypes`) — operator-written, never the user.
- `operator/main.py`: Kopf validate/create/update/delete handlers; imports cleanly without kopf.
- `config/otel-collector.yaml` (OTLP→Jaeger/Prometheus) + `config/otel-targets.yaml` (decoupled endpoint, host.k3d.internal:4317). *(collector config later moved to `examples/k3d-cluster-demo/` — it is demo-specific; `otel-targets.yaml` stays in `config/` as the general reference.)*
- `tests/test_operator_otel.py`: 7 tests — valid/invalid policies, model-seed, reconcile status, OTel schema conformance (TC-F-08: no drift.* keys, score on event in [0,1]).

Maps to: Implementation Plan S2; FR-4/5/6/9; Constraints C1; TC-F-01/02/08.

## S3 — Interceptor + runtime adapters
- `adapters/`: built-in `kagent` + `goose` (both normalize to the same DecisionChain — one policy governs both) + `custom_example` (the `custom` adapter path, FR-8). Registered on import via the SDK registry.
- `interceptor/engine.py`: transport-free `Interceptor` — normalize → score → enforce. Three actions: `log` (forward+flag), `drop` (silent 200 no-op), `block` (403 before kube-apiserver). Cold-start and exceptions fail to the declared `failurePolicy` (NFR-6). Emits the gen_ai.agent.* schema per call.
- `interceptor/server.py`: FastAPI sidecar (`/v1/tool-call`, `/healthz`) over the engine; FastAPI/uvicorn optional.
- `tests/test_interceptor_adapters.py`: 10 tests — kagent/goose same-shape, custom-by-name + builtin/ resolution, log/drop/block outcomes & status codes, happy-path forward, failClosed/failOpen resilience, cold-start failClosed.

Maps to: Implementation Plan S3; FR-1/7/8; NFR-1/6; TC-F-05/06/07/09/10/11.

## S4 — Demo stack (Helm + k3d + podman-compose + five scenarios)
- `cli.py`: `driftwatch demo <scenario>` runs all five scenarios through the real detection core (standalone — demo-safe, no cluster needed; identical in-cluster). Tiny SRE tool catalog (category/risk). `eval` subcommand wired to S5.
- `examples/k3d-cluster-demo/`: `k3d-config.yaml` (cluster, host.k3d.internal), `compose.yaml` (OTel Collector + Jaeger + Prometheus + Grafana + Neo4j on podman-compose), `grafana-dashboard.json` (agent-decisions: gate.action, score.value p95, anomaly kinds, FP rate), README + DEMO_RUNBOOK (35-min beat sheet) + recordings/ placeholder.
- `config/prometheus.yaml`: scrape the collector's drift metrics. *(later moved to `examples/k3d-cluster-demo/` — demo-specific.)*
- `deploy/helm/driftwatch/`: Chart + values + values-k3d (OTLP→host.k3d.internal) + templates (operator Deployment, RBAC ClusterRole/binding, CRD install). CRD vendored into the chart.
- `make demo-1..5` / `cluster-up` / `obs-up` / `deploy` all wired. *(later split into a demo-local Makefile under `examples/k3d-cluster-demo/`; the root Makefile keeps only project-wide targets install/test/lint/eval/clean + a `demo` shortcut.)*
- `tests/test_cli_demos.py`: all five scenarios pass with correct anomaly.kind + action (tool→baseline_mismatch/block, scope→scope_creep/block, sequence→blocked_transition/drop, arg→arg_schema_novel/block, storm→drop).
- Fix: demo interceptor shares the baseline's tool catalog so category/risk match (prevents spurious risk-escalation); sequence drift attributed to the destination tool.

Maps to: Implementation Plan S4; Benefits §1; NFR-5; TC-D-02..07 end-to-end.

## S5 — Evaluation harness + dataset
- `evaluation_runner.py`: reads the `Prompt → Baseline → Toolchain → Deviation` JSONL, builds per-task baselines from the happy rows, scores every row, and reports **recall** (drift rows), **false-positive rate** (happy rows), **p95 scoring latency**, and the **inverse-scaling** OLS (β₁>0 ⇒ bigger models drift more). `make eval` prints the DATA-READY block.
- `evaluation/datasets/drift.jsonl`: 112-row synthetic **seed** (8 model tiers × 6 tasks happy + drift across ambiguity v3/v4). Field names map 1:1 to the OTel schema. Seed is cleanly separable (recall≈100%, FP≈0%) — real headline numbers come from cluster-captured chains.
- `evaluation/README.md`: dataset schema ↔ OTel mapping; seed-vs-real distinction.
- `cli.py eval` wired to the runner.
- `tests/test_eval_harness.py`: dataset shape, metric ranges, inverse-scaling computed (n≥20, β₁>0), summary renders.

Maps to: Implementation Plan S5; NFR-3; inverse-scaling; abstract DATA-READY slot.

## S6 — Polish
- `Docs/fp-tuning-runbook.md`: the NFR-3 procedure — tune a false positive out via window/threshold/dryRun in shadow mode, then promote to block (the "one we got wrong" beat).
- `Docs/adapter-guide.md`: how to write a runtime adapter against the SDK (FR-8) — ~10 lines, inherit all four-feature scoring + OTel emission.
- `Docs/architecture.md`: implementation architecture, module map, the decision-not-API-object invariant, OTel C1 compliance.
- `examples/k3d-cluster-demo/recordings/README.md`: asciinema fallback workflow for all five scenarios.
- Full suite green in ~/venv across all sprints.

> Note on counts: the per-sprint "N total green" figures in earlier entries were
> projected while test collection was briefly broken (db import bug, fixed in
> 57e11e2). The verified full-suite total is **28 passed** — see S6.1 below.

Maps to: Implementation Plan S6; NFR-3; FR-8; Constraints C1.

## S6.1 — Consultant review fixes
Five findings from the implementation review, all closed:
- **CI green:** removed the unused `defaultdict` import in `evaluation_runner.py` (`ruff check src tests` now passes — CI no longer fails).
- **Interceptor entrypoint:** added `interceptor/main.py` with `run()` (+ `build_default_interceptor`, OTLP endpoint from env); `pyproject` `driftwatch-interceptor = driftwatch.interceptor.main:run` now resolves. `server.py` keeps only `build_app`.
- **OTel fingerprint emission:** `Decision` now carries `observed_scope/category/arg_hash/risk`; `build_span_attributes` emits `gen_ai.agent.tool.category`, `gen_ai.agent.tool.parameters_hash`, `gen_ai.agent.tool.risk_severity` alongside `gen_ai.tool.name` (full fingerprint on the span, Constraints C1).
- **Helm sidecar path:** added `templates/sidecar-injector.yaml` (mutating webhook → injector Deployment + Service + MutatingWebhookConfiguration, gated by `webhook.enabled`, **off by default** in v1alpha1) + `deploy/sidecar-manual.yaml` (the supported no-webhook path: copy the interceptor sidecar block into the agent pod). `helm template` renders both modes.
- **Eval honesty:** `summary()` now prints "seed-validation numbers — do NOT use for the CFP headline" instead of "fill DATA-READY slot".

Suite: 28 passed; ruff clean; helm renders.

## S6.2 — GHCR publishing
- `Dockerfile`: multi-stage (build wheel → slim runtime); installs `[operator,interceptor]` extras; non-root; one image, two entrypoints (`driftwatch-operator` default, `driftwatch-interceptor` for the sidecar). Verified: `docker build` succeeds, all three console scripts resolve, deps import.
- `.dockerignore`: keep the image lean (no tests/docs/data/examples).
- `.github/workflows/release.yml`: build + push to `ghcr.io/<owner>/driftwatch` on main and `v*` tags, using the built-in `GITHUB_TOKEN` (packages: write) — no PAT.
- `Docs/publishing-ghcr.md`: automated (Actions) and manual push paths, plus the one-time "make package public" step.

Maps to: the cluster-deployable gap called out in the review — image now builds and has a publish path.

## S7 — k3d bring-up & hardening (first real cluster run)
Standing the demo up end-to-end on a live k3d cluster surfaced a chain of bugs the unit
suite couldn't catch — each a "builds/looks-wired but never actually run" gap. All fixed,
with a regression test where testable:
- **compose**: observability images fully-qualified (`docker.io/...`, podman won't resolve
  short names) + bumped to the Obs-Summit tested set (collector 0.151.0, jaeger 1.76.0,
  prometheus v3.11.3, grafana 11.6.14, neo4j 5.26-community).
- **operator startup** (a CrashLoop chain, each bug hidden behind the previous):
  - `kopf.cli.main()` → `AttributeError` (kopf.cli not auto-imported) → embedded `kopf.run(standalone=True)`.
  - admission handler demanded a webhook server that's off by default → `@kopf.on.validate` opt-in via `DRIFTWATCH_ADMISSION`.
  - kopf auth "ran out of credentials" → pin `kubernetes<31` + add `pykube-ng`.
  - reconcile `Permission denied: 'data'` (non-root sqlite write) → `DRIFTWATCH_DATA_DIR` env + chart emptyDir at `/data`.
- **interceptor**: `/v1/tool-call` returned 422 (FastAPI read the `Response` annotation as a
  query param under `from __future__ import annotations`) → status via `JSONResponse`; added HTTP-layer tests.
- **telemetry**: demo `Emitter` wired to `DRIFTWATCH_OTLP_ENDPOINT`; OTLP gRPC forced `insecure`
  for the plaintext collector (was failing SSL `WRONG_VERSION_NUMBER`). Spans verified in Jaeger (service `driftwatch`).
- **metrics + Grafana**: emit OTLP metrics (`driftwatch_decisions_total` / `_anomaly_total` /
  `_score_value_*`); provision Grafana datasources (Prometheus + Jaeger) + dashboard so panels render real data.
- **eval results**: `make eval --out` writes `drift_inverse_scaling.{json,txt}` + `drift_rows.jsonl`
  (sre-incident-demo layout); seed run flagged "do NOT use for headline" (β₁=0 — seed has no
  capability signal). Fixed root Makefile `eval` PYTHONPATH.
- **kagent reality**: real Kagent is Helm-installed + controller-managed (not a hand-authored
  Deployment) → reframed `sample-agents.yaml` as a path-A stand-in, documented the path-B MCP-hop
  integration; repaired `sidecar-manual.yaml`.
- **docs**: `examples/k3d-cluster-demo/SETUP_RUNBOOK.md` (observability → cluster → install → data,
  + cross-runtime OTLP-link troubleshooting); `Docs/consensus-and-mcp-proxy-plan.md` (FR-9
  consensus-seed producer + E7 MCP-proxy; TC-F-16/17 E7, TC-F-18/19 consensus); publishing-ghcr
  switched to podman (docker noted equivalent).

Verified end-to-end on k3d: GHCR public image pulled remotely (~2s), operator `1/1 Running`
reconciling policy status, `make demo-all` → 5/5 with `gen_ai.agent.*` in Jaeger and metrics
in Prometheus/Grafana.

> Status: v1alpha1 reference implementation, **validated end-to-end on k3d**. Next: FR-9
> consensus-seed producer + E7 MCP-proxy enforcement (planned in
> `Docs/consensus-and-mcp-proxy-plan.md`). Go/controller-runtime rewrite remains roadmap
> once the CRD contract stabilizes.
