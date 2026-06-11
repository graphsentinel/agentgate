# config/

General, reusable reference — not tied to any one deployment. Nothing here is mounted
or executed automatically.

| File | What | Used by |
|---|---|---|
| `otel-targets.yaml` | The decoupling reference — **where DriftWatch pushes OTLP** (one endpoint; backend in-cluster / podman-compose / cloud). The single place documenting "telemetry destination is config, not code." | Cited by `deploy/README.md`, `deploy/sidecar-manual.yaml`, and the demo's `k3d-config.yaml` / `otel-collector.yaml`. |

## Policies?

There is **one** policy set, in the runnable demo:
[`../examples/k3d-cluster-demo/manifests/`](../examples/k3d-cluster-demo/manifests/)
(`agentdriftpolicy-shadow.yaml` → `action: log`, `agentdriftpolicy-enforce.yaml` →
`action: block`). Copy and adapt those for your own cluster — change the `selector`,
namespace, and `observability.otel.endpoint`. The full field reference is the CRD schema
in [`../deploy/crd/agentdriftpolicy.yaml`](../deploy/crd/agentdriftpolicy.yaml).
