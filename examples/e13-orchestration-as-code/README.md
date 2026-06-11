# AgentGate demo — orchestration as code, end to end

A `planner → coder → reviewer` org, declared as code and run as one pod, with optional live LLM
(Ollama) and full telemetry (Jaeger / Prometheus / Grafana). This runbook brings it up from scratch.

## Files here

| File | What |
|---|---|
| `org.yaml` | The org (CR form) for the `agentgate generate` / `run` CLI. |
| `with-tools.yaml` | An agent that actually calls a tool (the built-in `calculator`) — creation-driven tool binding. |
| `values-live.yaml` | Helm override: real model (Ollama) + dynamic gate + OTLP endpoint. |
| `values-violation.yaml` | A `planner→reviewer` breach demo + a telemetry attribute allow-list. |
| `demo.py` | The same pipeline as a standalone Python script. |
| `data/` | Bind-mount target for the observability stack's volumes (metrics/traces persist across restarts). |

---

## A. Quick path — local CLI, no cluster

```bash
pip install -e "../../.[codegen]"          # langgraph

# stub mode (no model — proves the wiring)
agentgate run org.yaml --goal "reverse a string"

# live (Ollama on the host)
AGENTGATE_LLM_PROVIDER=ollama agentgate run org.yaml --dynamic --goal "is 17 prime?"
```

---

## B. Full path — Kubernetes (k3d) + observability

Prereqs: a k3d cluster (`driftwatch-demo`), `kubectl`, `helm`, `podman` + `podman-compose`, and
(for live LLM) Ollama on the host.

### 1. Observability stack (host, podman-compose) — with persistent `data/`

The stack lives in `../k3d-cluster-demo/compose.yaml` (OTel Collector, Jaeger, Prometheus, Grafana
+ the pre-provisioned **“AgentGate — runs & governance”** dashboard). Bring it up:

```bash
make -C ../k3d-cluster-demo obs-up
# Collector :4317 (OTLP gRPC) · Jaeger :16686 · Prometheus :9090 · Grafana :3000
```

**Persist the data under this demo's `data/`** (so metrics/traces survive a restart) by bind-mounting
the container volumes — add to the relevant services in `compose.yaml`:

```yaml
  prometheus:
    volumes:
      - ./../e13-orchestration-as-code/data/prometheus:/prometheus
  grafana:
    volumes:
      - ./../e13-orchestration-as-code/data/grafana:/var/lib/grafana
  # jaeger (badger storage):
  jaeger:
    environment: [ SPAN_STORAGE_TYPE=badger, BADGER_EPHEMERAL=false,
                   BADGER_DIRECTORY_VALUE=/badger/data, BADGER_DIRECTORY_KEY=/badger/key ]
    volumes:
      - ./../e13-orchestration-as-code/data/jaeger:/badger
```

```bash
mkdir -p data/{prometheus,grafana,jaeger}     # the bind-mount targets (kept by data/.gitkeep)
```

### 2. CoreDNS / host alias — let pods reach host services by name

In-cluster pods reach the **host's Ollama (:11434)** and **OTel Collector (:4317)** via
`host.k3d.internal`. k3d usually injects it automatically; if not, register it in CoreDNS:

```bash
make -C ../k3d-cluster-demo host-alias OLLAMA_HOST=<host-ip-or-name>   # adds host.k3d.internal
# verify from a pod:
kubectl run nettest --rm -i --restart=Never --image=curlimages/curl -- \
  curl -s http://host.k3d.internal:4317        # collector reachable (gRPC; empty body is fine)
```

### 3. Install AgentGate (Helm)

```bash
helm install agentgate ../../deploy/helm/agentgate -n agentgate --create-namespace \
  -f values-live.yaml
# (from the public registry instead of the local chart:
#  helm install agentgate oci://ghcr.io/graphsentinel/charts/agentgate --version 0.1.0 \
#    -n agentgate --create-namespace -f values-live.yaml )

kubectl -n agentgate rollout status deploy/agentgate-agentgate
```

`values-live.yaml` already sets `dynamic: true`, `otel.endpoint: host.k3d.internal:4317`, and the
Ollama env — edit the model ids to whatever your Ollama serves.

### 4. Run it — HTTP `/run` via the coordinator

```bash
kubectl -n agentgate port-forward svc/agentgate-agentgate 8088:8000 &

curl -s localhost:8088/ | jq                          # { agents, coordinator, dynamic }
curl -s -X POST localhost:8088/run -H 'content-type: application/json' \
  -d '{"goal":"Write a Python function that checks if a number is prime"}' | jq
```

You command the **coordinator** (`planner`); it delegates down the declared graph
(`planner→coder→reviewer`) and returns the run trace (`history` + any `violations`).

### 5. See the telemetry

```bash
# Jaeger — traces
open http://localhost:16686        # Service = agentgate → agent.run … ; filter violations with
                                   # Tag: gen_ai.agent.gate.declared=true
# Grafana — dashboards
open http://localhost:3000         # "AgentGate — runs & governance" (anonymous admin)
```

---

## C. Configure — change the org

The org is `values.org` in the override file. Edit it (add an agent, change a delegation, add a
`deny` rule, swap a model, tune `observability.otel.attributes`) and upgrade — a checksum annotation
rolls the pod so the new spec is read; the image never changes:

```bash
helm upgrade agentgate ../../deploy/helm/agentgate -n agentgate -f values-live.yaml
```

---

## D. Demo the governance (a real violation)

`values-violation.yaml` instructs the planner to hand off straight to the reviewer — but only
`planner→coder` is declared. The runtime gate catches the undeclared `planner→reviewer` hand-off:

```bash
helm upgrade agentgate ../../deploy/helm/agentgate -n agentgate -f values-violation.yaml
kubectl -n agentgate rollout status deploy/agentgate-agentgate
# run a few times; when the model picks the undeclared edge:
curl -s -X POST localhost:8088/run -H 'content-type: application/json' \
  -d '{"goal":"add two numbers"}' | jq '.violations'
#  → "delegation 'planner' -> 'reviewer' is not in the declared graph (novel edge)"
```

It shows as `agentgate_decisions_total{gate_action="block"}` +
`agentgate_anomaly_total{anomaly_kind="delegation_violation"}` (Grafana) and a `gate.declared=true`
span (Jaeger). `values-violation.yaml` also sets a telemetry **allow-list**
(`attributes: [id, task_type, gate.declared]`) — so those spans carry only those attributes, a live
demo of CRD-configurable telemetry.

---

## Tear down

```bash
helm uninstall agentgate -n agentgate
make -C ../k3d-cluster-demo obs-down
```
