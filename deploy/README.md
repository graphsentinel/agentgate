# Deploying DriftWatch

Install the governance plane into a cluster, then drive it with an `AgentDriftPolicy`.
The Helm chart installs the **CRD + operator + RBAC** together; a policy turns it on;
a sidecar puts the interceptor on the agent's tool path.

> Publishing the image/chart to GHCR is a separate, maintainer task — see
> [`../Docs/publishing-ghcr.md`](../Docs/publishing-ghcr.md). This page is for
> **consumers** installing a published DriftWatch.

## What's in `deploy/`

| Path | What |
|---|---|
| `helm/driftwatch/` | the Helm chart (CRD, operator, RBAC, optional webhook injector) |
| `crd/agentdriftpolicy.yaml` | the raw CRD manifest, for `kubectl apply` without Helm |
| `sidecar-manual.yaml` | manual interceptor-sidecar injection (the supported path in v1alpha1) |

## Prerequisites

- A cluster (`kind`/`k3d` for the demo, any Kubernetes ≥1.26 otherwise).
- `helm` ≥ 3.8 (OCI support).
- An OTLP collector reachable from the cluster — see
  [`../config/otel-targets.yaml`](../config/otel-targets.yaml). For the demo it runs in
  podman-compose on the host and the cluster reaches it at `host.k3d.internal:4317`.

## 1. Install the chart (CRD + operator + RBAC)

From the published OCI registry — no clone needed:

```bash
helm install driftwatch oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0 \
  --namespace driftwatch --create-namespace \
  --set otel.endpoint=host.k3d.internal:4317
```

Or from a local checkout:

```bash
helm install driftwatch deploy/helm/driftwatch \
  -f deploy/helm/driftwatch/values-k3d.yaml \
  --namespace driftwatch --create-namespace
```

Confirm the CRD and operator are up:

```bash
kubectl get crd agentdriftpolicies.driftwatch.graphsentinel.org
kubectl -n driftwatch get pods            # operator Running
kubectl -n driftwatch get adp             # AgentDriftPolicy (adp) — none yet
```

> CRD-only, without Helm:
> `kubectl apply -f deploy/crd/agentdriftpolicy.yaml`

## 2. Apply a policy — shadow first, then enforce (NFR-5)

There is one ready-to-run policy set in the demo; copy and adapt for your cluster
(change `selector`, namespace, and `observability.otel.endpoint`):

```bash
# shadow: scores and emits OTel, blocks nothing — build trust
kubectl apply -f examples/k3d-cluster-demo/manifests/agentdriftpolicy-shadow.yaml
kubectl get adp demo-shadow -o jsonpath='{.status}{"\n"}'   # baselineReady, observedTaskTypes

# once you trust the baseline, flip to enforcement
kubectl apply -f examples/k3d-cluster-demo/manifests/agentdriftpolicy-enforce.yaml
```

`status` is written by the operator — you never set it. `action` is the one knob:
`log` (shadow) → `drop`/`block` (enforce). Field reference: the CRD schema in
[`crd/agentdriftpolicy.yaml`](crd/agentdriftpolicy.yaml).

## 3. Govern an agent pod (sidecar)

Add the interceptor sidecar to the agent so its tool calls are scored before they leave
the pod. In v1alpha1 this is **manual** (the webhook injector is roadmap, off by
default):

```bash
kubectl apply -f deploy/sidecar-manual.yaml
```

Copy the `driftwatch-interceptor` container block from that file into any agent
Deployment, and point the agent's tool/MCP client at `http://localhost:8080/v1/tool-call`.

To enable the automatic mutating-webhook injector once its image ships:

```bash
helm upgrade driftwatch oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0 \
  --reuse-values --set webhook.enabled=true
# then label pods to inject: driftwatch.graphsentinel.org/inject="true"
```

## Configuration reference (`values.yaml`)

| Key | Default | Purpose |
|---|---|---|
| `image.repository` / `image.tag` | `ghcr.io/graphsentinel/driftwatch` / `0.1.0a0` | the one image (operator + interceptor) |
| `otel.endpoint` | `otel-collector.observability.svc...:4317` | **decoupled** OTLP target; override per env (`values-k3d.yaml` → `host.k3d.internal:4317`) |
| `otel.protocol` | `grpc` | `grpc` \| `http/protobuf` |
| `crd.install` | `true` | install the `AgentDriftPolicy` CRD with the chart |
| `rbac.create` | `true` | operator ClusterRole + binding |
| `webhook.enabled` | `false` | sidecar-injector mutating webhook (roadmap; use the manual sidecar until then) |
| `operator.resources` / `interceptor.resources` | small | requests/limits |

Config never lives in the image — it comes from these values and the `AgentDriftPolicy`
CRD at deploy time, so the same published image runs anywhere.

## Verify end to end

```bash
# a drifting tool call against an enforcing policy returns 403 before the API,
# and a gen_ai.agent.* span + gen_ai.evaluation.result event reach your collector.
kubectl -n driftwatch logs deploy/driftwatch-operator | grep -i baselineReady
```

For the full on-stage walkthrough see
[`../examples/k3d-cluster-demo/DEMO_RUNBOOK.md`](../examples/k3d-cluster-demo/DEMO_RUNBOOK.md).

## Uninstall

```bash
helm uninstall driftwatch -n driftwatch
# Helm leaves CRDs by design; remove explicitly if you want them gone:
kubectl delete crd agentdriftpolicies.driftwatch.graphsentinel.org
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `status.baselineReady: false` | not enough runs folded yet (cold start) | feed `sources` / wait for `window` runs; calls follow `failurePolicy` meanwhile |
| every call blocked right after install | cold start + `failClosed` + `action: block` | start in `shadow-mode.yaml` (`action: log`) first |
| no spans in your backend | wrong `otel.endpoint` | check `config/otel-targets.yaml`; from k3d use `host.k3d.internal:4317` |
| legitimate call blocked | false positive | see [`../Docs/fp-tuning-runbook.md`](../Docs/fp-tuning-runbook.md) — tune `window`/`threshold`/`dryRun` in shadow, then promote |
