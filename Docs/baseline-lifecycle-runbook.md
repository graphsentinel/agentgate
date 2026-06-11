# Baseline lifecycle runbook

How a DriftWatch baseline comes to exist, becomes ready, resists poisoning, survives a
restart, and graduates from shadow to enforcement — end to end, in one place.

The key mental model, and the one to say out loud in a demo: **persistence does not
*create* a baseline; it only keeps one from being lost on restart.** A baseline is *built*
from trusted decision chains (real successful runs / approved traces / dry-runs) or
*bootstrapped* from an offline model-panel consensus seed. "I enabled the PVC and it went
ready" is the wrong story; "the baseline is learned from trusted chains, and the PVC keeps
it across restarts" is the right one.

---

## 1. The five states of a baseline

| State | What's true | `status` |
|-------|-------------|----------|
| **cold-start** | No chains folded yet for a task type. The interceptor cannot score. | `baselineReady: false`, `observedTaskTypes: 0` |
| **warming** | Some trusted chains folded, but `< window`-minimum runs. | `baselineReady: false` |
| **ready** | `runs >= 2` for a task type (`TaskBaseline.ready`); scoring is meaningful. | `baselineReady: true`, `observedTaskTypes: N` |
| **persisted** | The ready baseline is on a PVC (`persistence.enabled=true`), so it survives operator restarts. | unchanged; just durable |
| **enforcing** | Policy `action` is `drop`/`block` (not `log`); drift is acted on. | unchanged; behavior differs |

Cold-start behavior is governed by `failurePolicy`: `failClosed` blocks until the baseline
is ready (safe default), `failOpen` forwards. So an empty baseline is never silently
"allow everything" unless you asked for it.

---

## 2. Where a baseline comes from — two ingestion paths

### Path A — trusted fold from real runs (the steady state)

`baseline.sources` names which chains are trusted to widen "normal". Only
`approvedTraces`, `successfulRuns`, and `dryRun` auto-fold — this is the **poisoning guard**
(NFR-8): an untrusted source, or a chain the *current* baseline already scores as drift,
is refused. A drifting chain can never redefine normal by being observed.

```
sources: ["approvedTraces", "successfulRuns"]   # demo-shadow policy
```

### Path B — offline consensus seed (cold-start bootstrap, FR-9)

When a fresh cluster has no traces, seed from a model panel. The producer is **offline** —
the operator never calls an LLM. You collect proposals (one list of tool chains per model),
then distill a multi-granularity quorum baseline and fold it:

```bash
# proposals.json:  {"<task>": {"<model>": [["toolA","toolB"], ...], ...}, ...}
driftwatch consensus-seed \
  --proposals proposals.json \
  --policy   policy.json \
  --out      ./seed-out          # writes consensus_seed.json provenance here
```

A tool only one model proposes is dropped; a combined chain no model proposed is never
admitted (quorum at tool/scope/transition/chain-template level); a task with `< 2` voices
is refused, not seeded from a single voice. The survivors fold exactly like trusted runs.

---

## 3. Demo / review flow (deterministic, reproducible)

This is the clean on-stage sequence — bootstrap first, *then* show readiness, *then*
graduate. It does not depend on live LLMs.

```bash
# 0. install the governance plane, persistence ON so the baseline is durable
helm install driftwatch oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0 \
  --namespace driftwatch --create-namespace \
  --set persistence.enabled=true \
  --set-string image.digest="sha256:<pinned-digest>"   # see Docs/publishing-ghcr.md

# 1. apply the shadow policy (action: log — score + emit, never block)
kubectl apply -f examples/k3d-cluster-demo/manifests/agentdriftpolicy-shadow.yaml

# 2. make the baseline ready — bootstrap from trusted chains (or a consensus seed).
#    In-process this is what `make demo` exercises; in-cluster, drive the sample
#    workload so successful runs fold into the baseline.
make demo                              # five scenarios against a real, folded baseline

# 3. confirm readiness (status is written by the operator, never by you)
kubectl get agentdriftpolicies.driftwatch.graphsentinel.org -n driftwatch \
  -o custom-columns='NAME:.metadata.name,READY:.status.baselineReady,TYPES:.status.observedTaskTypes'
# READY should now be true with TYPES >= 1

# 4. prove persistence: restart the operator, baseline is still there
kubectl -n driftwatch rollout restart deploy/driftwatch-operator
kubectl -n driftwatch rollout status  deploy/driftwatch-operator
#    READY stays true (with persistence ON). With persistence OFF (emptyDir, the demo
#    default) it would reset to false — that is the NFR-10 point, shown deliberately.

# 5. graduate shadow -> enforce
kubectl apply -f examples/k3d-cluster-demo/manifests/agentdriftpolicy-enforce.yaml
#    now a drifting tool call is dropped/blocked per the policy action.
```

> If you skip step 2, READY stays `false` forever — that is correct, not a bug: there is
> nothing trusted to learn from yet. This is exactly what a freshly-restarted operator on
> an emptyDir baseline shows.

---

## 4. Poisoning guard — what it refuses (NFR-8)

`Reconciler.observe(chain, source=...)` is the single fold gate:

- **untrusted source** → never folds (returns refused).
- **cold start** (baseline not yet ready) → folds, to bootstrap (nothing to score against).
- **ready baseline** → folds **only if** the chain is within baseline; a chain the current
  baseline scores as drift is refused, so a drift-suspect or shadow-"would-have-blocked"
  chain cannot quietly become the new normal.

Net effect: the baseline only ever widens from chains that are both *trusted by source* and
*non-drifting against what's already learned*.

---

## 5. Persistence — durability, not creation (NFR-10)

`persistence.enabled=true` mounts the operator's `/data` (`DRIFTWATCH_DATA_DIR`) on a PVC
instead of an emptyDir, so the sqlite-backed baseline store survives pod restarts. Default
is OFF — the demo's emptyDir is intentional, and restarting the operator is the cleanest
way to *show* why durability matters.

The same store is the seam for the FR-10 operator→sidecar handoff: the operator writes the
reconciled baseline to `/data`, and a sidecar mounts the same store read-only plus the
policy knobs via env (`DRIFTWATCH_ACTION`/`THRESHOLD`/`FAILURE_POLICY`/`FEATURES`). This is
implemented, unit-tested (incl. a read-only-mount case), and **verified in-cluster** by
`examples/k3d-cluster-demo/fr10-e2e.sh` (TC-F-21): operator writes a ready baseline to the
PVC → a separate interceptor pod mounts it read-only → live `/v1/tool-call` forwards a
within-baseline call (200) and blocks a drift (403). (That e2e ran on the published
`:0.1.0a0` image; the sidecar now also opens the store with `SqliteBackend(read_only=True)`
as a hardening measure — rebuild/republish to sync the public image with the repo. The
read-only open is defensive, not load-bearing: the e2e passed without it because an
existing db dir is a no-op for `mkdir(exist_ok=True)` and `CREATE TABLE IF NOT EXISTS`.)
The remaining roadmap item is Helm-managed sidecar injection; until then use the manual
wiring in `deploy/sidecar-manual.yaml`.

```bash
# run the FR-10 in-cluster e2e (needs persistence enabled + KUBECONFIG)
examples/k3d-cluster-demo/fr10-e2e.sh
```

---

## 6. Quick reference

| You want to… | Do this |
|---|---|
| See if a baseline is ready | `kubectl get agentdriftpolicies… -o custom-columns=…status.baselineReady` |
| Bootstrap a cold cluster offline | `driftwatch consensus-seed --proposals … --policy … --out …` |
| Build from real runs | apply a policy with `sources: [approvedTraces, successfulRuns]`, drive the workload |
| Keep the baseline across restarts | `--set persistence.enabled=true` |
| Run safe-by-default before enforcing | start with `action: log` (shadow), then switch to `block`/`drop` |
| Confirm a drift chain didn't poison normal | it's refused at fold; only trusted non-drift chains widen the baseline |
