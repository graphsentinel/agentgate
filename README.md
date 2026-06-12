# AgentGate

**Declare a multi-agent organization as code — then generate, run, and govern it.**

You write one YAML: which agents exist, what each does, which tools it may use, who may hand work to
whom, and which orderings are forbidden. AgentGate turns that into a **running multi-agent
application** (LangGraph, AutoGen, or CrewAI) and **governs it** — so the agents can only do what you
declared, by construction at generation time and, optionally, checked again at run time.

```
one YAML  ──▶  generate  ──▶  run (one pod, /run)  ──▶  govern
(the org)      (a framework     a coordinator drives     creation-driven (rules baked in)
               app)             the declared graph       + observability-driven (runtime gate)
```

- **Framework-agnostic** — the same declaration generates a LangGraph, AutoGen, or CrewAI app. Change
  the target, not your org.
- **Governed by construction** — a forbidden hand-off is *never generated*; a cyclic or
  scope-widening org *fails to generate*. The rule can't be broken because the code to break it
  doesn't exist.
- **Run-time gate (optional)** — in *dynamic* mode the orchestrator chooses the next agent at run
  time and AgentGate checks each hand-off against the declared graph (undeclared / cyclic /
  scope-escalating → recorded and blocked).
- **Provider-agnostic LLM** — agents run on any model (Ollama, OpenAI-compatible, …); with no model
  configured they run in a deterministic stub mode that still proves the wiring.

> Apache-2.0. Ships as a public container image and Helm chart on GHCR — install with one command,
> no clone or build required.

---

## Table of contents

- [How it works](#how-it-works)
- [The org as code](#the-org-as-code)
- [Two ways to govern](#two-ways-to-govern)
- [Quick start (local, no cluster)](#quick-start-local-no-cluster)
- [Install on Kubernetes (Helm)](#install-on-kubernetes-helm)
- [Configuration](#configuration)
- [Observability (Jaeger / Prometheus / Grafana)](#observability-jaeger--prometheus--grafana)
- [Demo — `examples/e13-orchestration-as-code`](#demo--examplese13-orchestration-as-code)
- [Documentation](#documentation)

---

## How it works

Four stages over a single declared file:

| Stage | What happens |
|---|---|
| **Declare** | The agent org as code: per-agent `instructions` / `model` / `tools` / `scope`, the `delegations` graph (who may hand off to whom), and `rules` (forbidden orderings). |
| **Generate** | A per-framework generator turns it into runnable code — `delegations` → graph edges, `instructions` → prompt, `tools` → bound tools, the coordinator → entry point. The forbidden/undeclared edge is simply not emitted. |
| **Execute** | The generated app runs as **one pod**. You send a goal to **one entry point — the coordinator** (the top of the graph); it delegates down the declared graph and returns the result. You command the coordinator, not each agent. |
| **Govern** | *Creation-driven* (rules baked into the generated code) + optional *observability-driven* (each run-time hand-off scored against the declared graph). |

```
you ──"goal"──▶ POST /run ─────── one pod ─────────────┐
                    ▼                                    │
               coordinator (e.g. planner)               │
                    │ delegates per the declared graph   │
                    ├──▶ coder                           │
                    │       └──▶ reviewer                │
                    ◀── results bubble up                │
                    ▼                                    │
               final answer ─────────────────────────────┘
```

Agents come alive automatically when the pod starts (the framework loads them as in-process nodes) —
you don't start them individually. Everything runs **inside one framework, one pod**.

---

## The org as code

This is the whole declaration — agents, the delegation graph, and forbidden orderings:

```yaml
agents:
  - name: planner
    model: qwen3.5:9b
    instructions: |
      Break the goal into 3 short numbered steps. Output only the steps.
    tools: []
  - name: coder
    model: qwen3.5:9b
    instructions: |
      Implement step 1 as a short Python function. Output only the code.
    tools: [calculator]            # bound tools — the agent may use ONLY these
  - name: reviewer
    model: qwen3.5:9b
    instructions: |
      Point out exactly one concrete issue. Output 1-2 sentences.
    tools: []
delegations:                       # the DAG: who may hand off to whom
  - { from: planner, to: coder }
  - { from: coder, to: reviewer }
rules:                             # forbidden orderings (optional)
  - deny: [reviewer, coder]        # reviewer may never delegate back to coder
```

- `instructions` is the agent's actual job (its prompt). `tools` / `scope` / `delegations` are its
  **limits** — what it may use and where it may hand work. The first goes to the framework; the
  second is what AgentGate enforces.
- **Tool binding is creation-driven too:** an agent is only ever offered its bound tools at run time;
  a call to anything else is refused.

---

## Two ways to govern

| Mode | `dynamic` | What it catches | When |
|---|---|---|---|
| **Static** (default) | `false` | Nothing *can* drift — edges are fixed in the generated code, an undeclared hand-off is impossible by construction. | A fixed pipeline you fully control. |
| **Dynamic** | `true` | The orchestrator picks the next agent at run time; AgentGate scores each pick against the declared graph (novel-edge / cycle / scope-escalation → recorded + blocked). | A smart orchestrator that routes at run time. |

Both also record a per-run trace: which tools each agent called (and any refused), surfaced in the
`/run` response and the CLI output.

---

## Quick start (local, no cluster)

With the repo checked out (`pip install -e ".[codegen]"`):

```bash
# generate a runnable app from the org (pick the framework)
agentgate generate examples/e13-orchestration-as-code/org.yaml --target langgraph   # or autogen / crewai

# run it: send a goal to the coordinator (stub mode — no model needed)
agentgate run examples/e13-orchestration-as-code/org.yaml --goal "reverse a string"

# go live with a real model (Ollama here) + the run-time gate
AGENTGATE_LLM_PROVIDER=ollama \
  agentgate run examples/e13-orchestration-as-code/org.yaml --dynamic --goal "is 17 prime?"
```

---

## Install on Kubernetes (Helm)

Straight from the public OCI registry — no clone, no build:

```bash
helm install agentgate oci://ghcr.io/graphsentinel/charts/agentgate --version 0.1.0 \
  --namespace agentgate --create-namespace
```

```bash
kubectl -n agentgate get pods                                  # agentgate Running
kubectl -n agentgate port-forward svc/agentgate-agentgate 8088:8000 &
curl -s localhost:8088/                                        # agents + coordinator
curl -s -X POST localhost:8088/run -H 'content-type: application/json' \
  -d '{"goal":"reverse a string"}' | jq                        # run it
```

That installs stub mode (no model — proves the wiring). To go live, see configuration below.

> The image (`ghcr.io/graphsentinel/agentgate`) and chart (`oci://ghcr.io/graphsentinel/charts/agentgate`)
> are public — anyone can install without access to this repo. Maintainer publish flow →
> [`Docs/publishing-agentgate-ghcr.md`](Docs/publishing-agentgate-ghcr.md).

---

## Configuration

Nothing is baked into the image — the org and runtime settings are Helm values, so the same image
runs any org. Change `values.org` and `helm upgrade`: a checksum annotation rolls the pod so the new
spec is read. The image never changes.

| Value | Default | Purpose |
|---|---|---|
| `org` | a 3-agent example | The agent org as code (agents + delegations + rules) → mounted at `/etc/agentgate/org.yaml`. |
| `dynamic` | `false` | `true` = run-time delegation gate (observability-driven); `false` = static (creation-driven). |
| `env` | `[]` | The agents' LLM, e.g. `AGENTGATE_LLM_PROVIDER=ollama` + `AGENTGATE_OLLAMA_HOST`. None = stub mode. |
| `otel.endpoint` | `""` | OTLP gRPC endpoint (the OTel Collector). Set → telemetry; empty → just the `/run` trace. See [Observability](#observability-jaeger--prometheus--grafana). |
| `org.observability.otel.attributes` | all | Which `gen_ai.agent.*` attributes to emit: `["none"]` = none, `["*"]`/absent = all, a list = only those. |
| `image.repository` / `.tag` | `ghcr.io/graphsentinel/agentgate` / `0.1.0` | The AgentGate image. |
| `replicaCount` | `1` | Server replicas. |
| `service.type` / `.port` | `ClusterIP` / `8000` | The `/run` service. |
| `securityContext` | hardened | non-root (uid 10001), read-only rootfs, seccomp, drop-ALL. |
| `resources` | 50m–1 CPU / 128–512Mi | Requests / limits. |

A ready-made live override (real model + dynamic gate) ships at
[`examples/e13-orchestration-as-code/values-live.yaml`](examples/e13-orchestration-as-code/values-live.yaml).

---

## Observability (Jaeger / Prometheus / Grafana)

Set one value — `otel.endpoint` — and every agent run emits a **`gen_ai.agent.*`** span (and metrics)
to the OTel Collector, fanned out to **Jaeger** (traces), **Prometheus** + **Grafana** (metrics). No
endpoint → AgentGate just returns the in-`/run` trace; nothing leaves the pod.

```bash
# enable: point at the collector (host.k3d.internal:4317 from k3d, localhost:4317 on the host)
helm upgrade agentgate deploy/helm/agentgate -n agentgate --reuse-values \
  --set otel.endpoint=host.k3d.internal:4317
```

**What you get** (scope `agentgate`):
- **Per-run span** `agent.run <name>` — `gen_ai.agent.id` / `task_type` / `model`, bound `tools`, each
  call a `gen_ai.agent.tool.call` event; on a gated hand-off `gen_ai.agent.gate.declared=true` +
  `gen_ai.agent.computed.anomaly.kind=delegation_violation`.
- **Metrics** — `agentgate_decisions_total{gate_action}` (log/block), `agentgate_anomaly_total{anomaly_kind}`.

**Configurable attributes** — `org.observability.otel.attributes` selects what's emitted (trade
detail for cost/noise), C1-safe (always a subset of the fixed schema):

```yaml
org:
  observability:
    otel:
      attributes: ["*"]          # all (default) | ["none"] = telemetry off | ["gen_ai.agent.id", ...]
```

**The stack** ships with the demo — one command brings up Collector (`:4317`), Jaeger (`:16686`),
Prometheus (`:9090`), Grafana (`:3000`, with the **“AgentGate — runs & governance”** dashboard
pre-provisioned):

```bash
make -C examples/k3d-cluster-demo obs-up
```

- **Jaeger** (`localhost:16686`): Service = `agentgate` → `agent.run …` traces. Filter violations with
  the Tag `gen_ai.agent.gate.declared=true`.
- **Grafana** (`localhost:3000`, anonymous): the *AgentGate — runs & governance* dashboard — runs,
  gate actions (log/block), and delegation violations.

---

## Demo — `examples/e13-orchestration-as-code`

A planner → coder → reviewer org. Everything to install, configure, and run it lives in that
directory:

| File | What |
|---|---|
| `org.yaml` | The org (CR form) for the `agentgate generate` / `run` CLI. |
| `with-tools.yaml` | An agent that actually calls a tool (the built-in `calculator`) — creation-driven tool binding. |
| `values-live.yaml` | Helm override: real model (Ollama) + dynamic gate. |
| `demo.py` | The same pipeline as a standalone Python script. |

### 1. Install via Helm — live (real model + dynamic gate)

`values-live.yaml` points the agents at a model over Ollama (`host.k3d.internal` reaches the host's
Ollama from inside k3d automatically). Edit the `model:` ids to whatever your Ollama serves, then:

```bash
helm install agentgate deploy/helm/agentgate -n agentgate --create-namespace \
  -f examples/e13-orchestration-as-code/values-live.yaml
# (or from the registry: helm install agentgate oci://ghcr.io/graphsentinel/charts/agentgate \
#    --version 0.1.0 -n agentgate --create-namespace -f examples/e13-orchestration-as-code/values-live.yaml)

kubectl -n agentgate rollout status deploy/agentgate-agentgate
```

### 2. Configure — change the org, re-apply

The org is `values.org`. Edit it (add an agent, change a delegation, add a `deny` rule, swap a
model) and upgrade — the pod rolls and re-reads the spec. The image is untouched:

```bash
helm upgrade agentgate deploy/helm/agentgate -n agentgate \
  -f examples/e13-orchestration-as-code/values-live.yaml
```

### 3. Run the demo

```bash
kubectl -n agentgate port-forward svc/agentgate-agentgate 8088:8000 &

curl -s localhost:8088/ | jq            # { service, agents, coordinator, dynamic }

curl -s -X POST localhost:8088/run -H 'content-type: application/json' \
  -d '{"goal":"Write a Python function that checks if a number is prime"}' | jq
```

Expected (dynamic mode): the coordinator (`planner`) breaks down the goal and picks `NEXT: coder`;
`coder` writes the function and picks `NEXT: reviewer`; each hand-off passes the declared-graph gate
(`violations: []`); `reviewer` returns a review. Try an org that declares a forbidden or
scope-widening hand-off and watch it get recorded/blocked in `violations`.

> Tip: if host port 8000 is busy locally, port-forward to a different host port (e.g. `8088:8000`)
> as above — each pod uses its own 8000 inside the cluster.

---

## Reaching a host / remote LLM from the cluster (CoreDNS)

For **Ollama** the agents call `http://host.k3d.internal:11434`. In-cluster pods reach a host-local
*or* remote Ollama by that **stable name** — no hardcoded IP in any manifest. Register it once
(idempotent CoreDNS `NodeHosts` patch + CoreDNS restart):

```bash
# local Ollama on the k3d host (default) — discovers the host gateway from the node route
./examples/e13-orchestration-as-code/register-host-alias.sh driftwatch-demo

# remote Ollama by IP or DNS name (aliased to host.k3d.internal)
OLLAMA_HOST=10.0.0.42            ./examples/e13-orchestration-as-code/register-host-alias.sh
OLLAMA_HOST=ollama.corp.example  ./examples/e13-orchestration-as-code/register-host-alias.sh
```

Then `spec.llm.endpoint: http://host.k3d.internal:11434` resolves from every pod. **Cloud providers**
(openai-compatible / RunPod / anthropic / gemini / bedrock) use their public endpoints — no alias
needed; only host-local Ollama does.

---

## Documentation

| Doc | What |
|---|---|
| [`Docs/e13-mabac-delegation-design.md`](Docs/e13-mabac-delegation-design.md) | The full design: declare → generate → execute → govern, the two modes, the runtime gate. |
| [`Docs/publishing-agentgate-ghcr.md`](Docs/publishing-agentgate-ghcr.md) | Maintainer: build + push the image and chart to GHCR (public). |
| [`examples/e13-orchestration-as-code/`](examples/e13-orchestration-as-code/) | The demo org, tool example, live Helm values, and the `register-host-alias.sh` CoreDNS script. |
| [`deploy/helm/agentgate/`](deploy/helm/agentgate/) | The Helm chart (values, deployment, service, configmap). |

Apache-2.0.
