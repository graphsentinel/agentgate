# AgentGate — Installation Guide

AgentGate declares a multi-agent organization as code (an **AgenticArchitecture**) and
**generates + runs** it as a service, governed by construction. This guide covers every supported
way to install and run it, from a laptop to a Kubernetes cluster.

For every configuration knob, see **[Configuration.md](Configuration.md)**. When something doesn't
come up, see **[Troubleshooting.md](Troubleshooting.md)**.

---

## 1. Prerequisites

| Need | For | Notes |
|---|---|---|
| Python ≥ 3.12 | local CLI / dev | only for the `pip install` path |
| Podman **or** Docker | container run / build | commands are identical; examples use `podman` |
| Helm ≥ 3.8 | Kubernetes install | OCI registry support (3.8+) is required for `helm install oci://…` |
| A Kubernetes cluster (k3d / kind / any) | the Helm path | k3d/kind is fine for dev |
| An LLM endpoint (optional) | live agents | Ollama, OpenAI-compatible, Anthropic, Gemini, Bedrock, RunPod. **Omit to run in deterministic stub mode** (proves wiring, no model needed) |

Artifacts are published to GHCR (owner `graphsentinel`):

| Artifact | Reference |
|---|---|
| Container image | `ghcr.io/graphsentinel/agentgate:0.1.0` |
| Helm chart (OCI) | `oci://ghcr.io/graphsentinel/charts/agentgate:0.1.0` |

---

## 2. Quick start — local CLI (no cluster)

```bash
pip install -e '.[server,codegen]'          # from a checkout; add ,mcp for external MCP tools
# generate + inspect the declared app
agentgate examples/e13-orchestration-as-code/org.yaml
# run it (HTTP service)
AGENTGATE_SPEC_PATH=examples/e13-orchestration-as-code/org.yaml agentgate-server
```

`agentgate` is the generate/run CLI; `agentgate-server` is the FastAPI `/run` service (needs the
`server,codegen` extras).

---

## 3. Container

The image's entrypoint is `agentgate-server` (port 8000). The agent org is **not baked in** — it is
mounted at `AGENTGATE_SPEC_PATH` (default `/etc/agentgate/org.yaml`).

```bash
# pick a FREE host port — 8000 is often taken (see Troubleshooting); here we map 8001->8000
podman run -d --name agentgate -p 8001:8000 \
  -v $PWD/examples/e13-orchestration-as-code/org.yaml:/etc/agentgate/org.yaml:ro \
  ghcr.io/graphsentinel/agentgate:0.1.0

# verify (note: /run needs an application/json content-type)
curl -s localhost:8001/ | jq                       # agents + coordinator
curl -s -X POST localhost:8001/run \
  -H 'content-type: application/json' \
  -d '{"goal":"reverse a string"}' | jq
```

Go live with a model by adding env (see [Configuration.md](Configuration.md) §LLM), e.g. Ollama:

```bash
podman run -d --name agentgate -p 8001:8000 \
  -v $PWD/examples/.../org.yaml:/etc/agentgate/org.yaml:ro \
  -e AGENTGATE_LLM_PROVIDER=ollama \
  -e AGENTGATE_OLLAMA_HOST=http://host.containers.internal:11434 \
  ghcr.io/graphsentinel/agentgate:0.1.0
```

---

## 4. Kubernetes (Helm)

The chart renders a Deployment + Service + a ConfigMap holding your org (mounted at
`/etc/agentgate/org.yaml`). Install straight from the OCI registry:

```bash
helm install checkout oci://ghcr.io/graphsentinel/charts/agentgate --version 0.1.0 \
  --set app=checkout \
  -f my-values.yaml          # your org + LLM + dynamic flag (see Configuration.md)
```

Minimal `my-values.yaml`:

```yaml
app: checkout                 # multi-app id (push ref + _meta.app routing key); default = release name
dynamic: false                # static graph (creation-driven). true = runtime-gated delegation
otel:
  endpoint: ""                # e.g. otel-collector.observability.svc:4317 for telemetry
org:                          # THE AgenticArchitecture (bare ASL form) — see Configuration.md
  agents:
    - name: planner
      instructions: "Break the goal into 3 steps."
  # ...
```

Verify and call it:

```bash
kubectl get pods -l app.kubernetes.io/name=agentgate
kubectl port-forward svc/checkout-agentgate 8001:8000 &
curl -s -X POST localhost:8001/run -H 'content-type: application/json' \
  -d '{"goal":"is 17 prime?"}' | jq
```

**Change the org** = edit `values.org` (or `-f`/`--set`) and `helm upgrade`. A checksum annotation
rolls the pod so the new spec is read. The image never changes.

---

## 5. Governing AgentGate with DriftWatch (optional)

To route this app's tool calls through a DriftWatch governance plane, set `govern.proxyType:
driftwatch` in the AgenticArchitecture (`org`). At startup AgentGate pushes its contract once and
binds every agent to the proxy. The app id (`app`) becomes the contract `ref` and the `_meta.app`
routing key, so several AgentGates can share one DriftWatch. See [Configuration.md](Configuration.md)
§Governance and DriftWatch's own manual.

```yaml
org:
  govern:
    proxyType: driftwatch
    endpoint: http://driftwatch-mcp.driftwatch.svc:8000/mcp   # governed tool path
    register: http://driftwatch-mcp.driftwatch.svc:8000/contracts  # one-time contract push
```

---

## 6. Reaching a host / remote LLM from the cluster (CoreDNS)

If agents must call an Ollama running on the host (or a remote box) by a stable name, register a
CoreDNS alias so pods resolve `host.k3d.internal`:

```bash
./examples/e13-orchestration-as-code/register-host-alias.sh <k3d-cluster-name>
# then set AGENTGATE_OLLAMA_HOST=http://host.k3d.internal:11434
```

The script is idempotent and re-points the alias if the host DHCP address changes. Cloud providers
(OpenAI/Anthropic/Gemini/RunPod) use public endpoints and need no alias.

---

## 7. Building the image yourself

```bash
podman build -t ghcr.io/graphsentinel/agentgate:0.1.0 .
# publish: see Docs/design/publishing-agentgate-ghcr.md (internal)
```

The Dockerfile installs the `server,codegen,mcp` extras and runs `agentgate-server` on 8000.

---

## 8. Verifying an install

| Check | Command | Expected |
|---|---|---|
| Service up | `curl -s localhost:<port>/healthz` | `{"status":"ok"}` |
| Org loaded | `curl -s localhost:<port>/` | `{"service":"agentgate","agents":[…],"coordinator":…}` |
| Run works | `POST /run` with JSON body | `{"coordinator":…,"history":[…],"violations":[]}` |

If `history` entries have no `output` field, you are in **stub mode** (no LLM provider set) — that is
expected and proves wiring; set the LLM env to go live.
