# Deploying AgentGate

Install AgentGate into a cluster: it renders an `AgenticArchitecture` (the agent org as code) into a
running multi-agent app and serves it over an HTTP `/run` endpoint. The Helm chart deploys the app
runner + the org as a ConfigMap; the LLM provider and key are config + a Secret.

> Publishing the image/chart to GHCR is a separate, maintainer task — see
> [`../Docs/publishing-agentgate-ghcr.md`](../Docs/publishing-agentgate-ghcr.md).

## What's in `deploy/`

| Path | What |
|---|---|
| `helm/agentgate/` | the Helm chart (deployment, service, configmap; org mounted at `/etc/agentgate/org.yaml`) |
| `crd/agenticarchitecture.yaml` | the `AgenticArchitecture` CRD (shared declare format; AgentGate generates from it, DriftWatch governs it) |

## Prerequisites

- A cluster (`k3d`/`kind` for the demo, any Kubernetes ≥1.26).
- `helm` ≥ 3.8.
- An LLM the pods can reach: **Ollama** via `host.k3d.internal:11434` (run
  [`../examples/e13-orchestration-as-code/register-host-alias.sh`](../examples/e13-orchestration-as-code/register-host-alias.sh)),
  or a cloud provider (openai-compatible / RunPod / anthropic / gemini / bedrock) by public endpoint.

## 1. Install the chart

```bash
helm install agentgate deploy/helm/agentgate \
  --namespace agentgate --create-namespace \
  -f examples/e13-orchestration-as-code/values-llm.yaml      # an org + an LLM config
```

Confirm the app is up and lists the org:

```bash
kubectl -n agentgate get pods                                  # agentgate Running
kubectl -n agentgate port-forward svc/agentgate-agentgate 8088:8000 &
curl -s localhost:8088/ | jq                                   # agents + coordinator
```

## 2. Configure the LLM (provider + key)

The org (`spec.llm` / per-agent `agent.llm`) sets `provider` / `model` / `endpoint`; the **API key is
a Secret**, never plaintext:

```yaml
# values.yaml (or the AgenticArchitecture spec.llm.apiKeySecretRef)
llm:
  apiKeySecretRef:
    name: llm-keys              # a Secret you created
    key: anthropic
    envName: ANTHROPIC_API_KEY  # exposed to the pod as <PROVIDER>_API_KEY
```
Providers: `ollama` (no key) | `openai`/`azure`/`runpod`/`vllm`/`tgi` (openai-compatible, `endpoint` =
base_url) | `anthropic` | `gemini` | `bedrock` (needs `agentgate[bedrock]` + AWS creds). For Ollama
register the CoreDNS alias (see the repo README).

## 3. Run

```bash
curl -s -X POST localhost:8088/run -H 'content-type: application/json' \
  -d '{"goal":"investigate the latency spike in checkout"}' | jq
```
A goal posted to the **coordinator** flows down the declared graph; each agent's run emits a
`gen_ai.agent.*` span (set `otel.endpoint` to your collector). `dynamic: true` enables the runtime
delegation gate.

## Configuration reference (`values.yaml`)

| Key | Default | Purpose |
|---|---|---|
| `image.repository` / `image.tag` | `ghcr.io/graphsentinel/agentgate` / `0.1.0` | the app-runner image |
| `dynamic` | `false` | runtime-gated dynamic delegation graph instead of static |
| `otel.endpoint` | "" | OTLP target for `gen_ai.agent.*` spans (e.g. `host.k3d.internal:4317`) |
| `org` | demo org | the `AgenticArchitecture` (agents, delegations, llm) rendered into a ConfigMap |
| `llm.apiKeySecretRef` | empty | LLM API key from a Secret → `<PROVIDER>_API_KEY` env |
| `env` | `[]` | extra env (e.g. `AGENTGATE_LLM_TIMEOUT`) |

Config never lives in the image — it comes from these values + the `AgenticArchitecture`, so the same
image runs anywhere.

## Optional: govern with DriftWatch

AgentGate produces the action; **DriftWatch governs it** (declared / baseline / cross-check). To put
the DriftWatch interceptor on an agent's tool path, see
[`../examples/integrations/driftwatch/sidecar-manual.yaml`](../examples/integrations/driftwatch/sidecar-manual.yaml)
and the DriftWatch repo. Point `spec.mcpServers[].url` at the DriftWatch proxy to route tool calls
through governance. Pure interop — AgentGate has no dependency on DriftWatch.

## Uninstall

```bash
helm uninstall agentgate -n agentgate
```
