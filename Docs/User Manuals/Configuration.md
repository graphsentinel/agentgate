# AgentGate — Configuration Reference

Every configuration surface AgentGate exposes: Helm values, environment variables, the
**AgenticArchitecture** spec (the org-as-code), LLM providers, and governance. Install steps are in
**[Installation.md](Installation.md)**; failure modes in **[Troubleshooting.md](Troubleshooting.md)**.

There are three layers, most-specific wins:
1. **AgenticArchitecture** (`org` / the mounted YAML) — *what* the agents are.
2. **Helm values** — *how* it's deployed (port, image, secrets, app id).
3. **Environment variables** — runtime floor (LLM provider, ports, OTLP); set by the chart or by hand.

---

## 1. Helm values

| Value | Default | Meaning |
|---|---|---|
| `image.repository` | `ghcr.io/graphsentinel/agentgate` | image repo |
| `image.tag` | `0.1.0` | image tag (= appVersion) |
| `image.pullPolicy` | `IfNotPresent` | |
| `replicaCount` | `1` | pod replicas |
| `app` | `""` (→ release name) | **multi-app id**: the contract push `ref` AND the `_meta.app` routing key for a central DriftWatch. Keep stable & unique per app |
| `dynamic` | `false` | `false` = static graph (creation-driven, undeclared hand-off impossible). `true` = runtime-gated dynamic delegation |
| `service.type` | `ClusterIP` | |
| `service.port` | `8000` | Service + container port; also sets `AGENTGATE_PORT` |
| `otel.endpoint` | `""` | OTLP gRPC target for `gen_ai.agent.*` telemetry (empty = no telemetry) |
| `env` | `[]` | extra env (the usual place for the LLM provider — see §LLM) |
| `llm.apiKeySecretRef.name` | `""` | Secret holding the LLM API key (empty = none, e.g. Ollama) |
| `llm.apiKeySecretRef.key` | `apiKey` | data key inside the Secret |
| `llm.apiKeySecretRef.envName` | `OPENAI_API_KEY` | env var the key is exposed as (set per provider) |
| `org` | sample 3-agent org | **the AgenticArchitecture** (bare ASL form) → ConfigMap at `/etc/agentgate/org.yaml` (see §3) |
| `securityContext.*` | hardened | non-root uid 10001, readOnlyRootFilesystem, drop ALL caps |
| `resources` | 50m/128Mi → 1/512Mi | requests/limits |

---

## 2. Environment variables

Set automatically by the chart, or by hand for `podman run` / local. LLM tuning vars accept a legacy
`DRIFTWATCH_`-prefixed fallback for back-compat.

| Var | Default | Meaning |
|---|---|---|
| `AGENTGATE_SPEC_PATH` | `/etc/agentgate/org.yaml` | path to the mounted AgenticArchitecture YAML |
| `AGENTGATE_DYNAMIC` | `false` | `true/1/yes` = dynamic graph |
| `AGENTGATE_PORT` | `8000` | HTTP port `run()` binds |
| `AGENTGATE_HOST` | `0.0.0.0` | bind host |
| `AGENTGATE_APP` | `""` | multi-app id (push ref + `_meta.app`); chart sets it from `app` |
| `AGENTGATE_OTLP_ENDPOINT` | `""` | OTLP gRPC endpoint for telemetry |
| `AGENTGATE_LLM_PROVIDER` | `""` | `ollama` \| `openai` \| `azure` \| `runpod` \| `vllm` \| `tgi` \| `openai-compatible` \| `anthropic` \| `gemini` \| `bedrock`. **Empty = stub mode** |
| `AGENTGATE_OLLAMA_HOST` | `http://localhost:11434` | Ollama base URL |
| `AGENTGATE_OPENAI_BASE_URL` | `https://api.openai.com/v1` | base_url for OpenAI-compatible/RunPod/vLLM/TGI |
| `AGENTGATE_LLM_TIMEOUT` | `180` | per-call LLM timeout (s) |
| `AGENTGATE_TOOL_ITERS` | `4` | max tool-loop iterations per agent turn |
| `AGENTGATE_MAX_TOKENS` | `1024` | max tokens (Anthropic/Bedrock) |
| `AGENTGATE_MCP_STRICT` | `false` | `true` = an unreachable MCP backend fails startup (prod readiness) instead of degrading |
| `AGENTGATE_PROMPTS_DIR` | `/etc/agentgate/prompts` | where `instructionsFrom.configMapKeyRef` resolves |
| `<PROVIDER>_API_KEY` | — | the LLM key, read by its **standard** name: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `RUNPOD_API_KEY` (never `AGENTGATE_`-prefixed). Supply via `llm.apiKeySecretRef` |
| `AWS_REGION` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | — | Bedrock only (`pip install agentgate[bedrock]`) |

---

## 3. The AgenticArchitecture (org-as-code)

The heart of AgentGate. Same shape whether it's a Kubernetes CR (`.spec`) or the bare ASL YAML in
`values.org`. Full field reference:

```yaml
# tools catalogue — declare once, bind per agent. risk feeds the governance risk_map.
tools:
  - { name: search,     category: web, risk: 0 }
  - { name: write_file, category: fs,  risk: 3 }

agents:
  - name: planner
    tier: strategic            # strategic | tactical | execution (informational)
    role: "one-line summary"
    model: "qwen3.5:9b"        # LLM id wired into this agent (or use llm.model below)
    instructions: |            # the agent's prompt body (opaque to governance)
      You are a planner...
    instructionsFrom:          # OR load the prompt from a file/ConfigMap (instructions wins if both)
      configMapKeyRef: { key: planner.txt }   # resolved under AGENTGATE_PROMPTS_DIR
      # path: /some/abs/path.txt
    tools: [search]            # bound tools (least-privilege; the model is only offered these)
    scope: ["ns:demo"]         # allowed scope prefixes ("" = unconstrained)
    canDelegateTo: [coder]     # declared delegation edges
    reportsTo: null            # hierarchy edge
    llm:                       # per-agent LLM override (else global llm, else env)
      provider: ollama
      model: qwen3.5:9b
      endpoint: http://host.k3d.internal:11434
    mcpServers:                # whole-backend tool binding (external MCP), least-privilege
      - { name: k8s, allow: ["pods_*"], deny: ["*_delete"] }

delegations:                   # edge-list sugar (folds into canDelegateTo)
  - { from: planner, to: coder }

rules:                         # declared deny-sequences (forbidden contiguous tool orderings)
  - { deny: [search, write_file], reason: "no write right after a web read", agent: coder }

govern:                        # E13 §4e — single-source interop with DriftWatch (see §5)
  proxyType: none              # none = standalone | driftwatch = push contract + route via proxy
  endpoint: ""                 # DriftWatch MCP proxy URL (governed tool path)
  register: ""                 # DriftWatch /contracts push URL
  app: ""                      # multi-app id override (else AGENTGATE_APP)

llm:                           # global LLM default (agents override per field)
  provider: ollama
  model: qwen3.5:9b
  endpoint: http://host.k3d.internal:11434
  apiKeySecretRef: { name: my-llm-secret, key: apiKey }   # CRD form; helm uses values.llm

observability:                 # which gen_ai.agent.* attributes to emit per run
  otel:
    attributes: ["*"]          # omit/[]=all | ["none"]=nothing | explicit list
```

### Static vs dynamic graph
- **`dynamic: false`** (default): edges are fixed in generated code — an undeclared hand-off is
  *impossible by construction*. Best for fixed pipelines.
- **`dynamic: true`**: the orchestrator picks the next agent at run time; `check_delegation` gates
  each pick against the declared graph (novel-edge / cycle / scope-escalation → recorded + blocked).
  `AGENTGATE_DELEGATION_ACTION` (default `block`) controls whether a violation drops the hand-off.

---

## 4. LLM providers

Set `AGENTGATE_LLM_PROVIDER` (or `llm.provider`) + a `model`. Keys come from a Secret via
`llm.apiKeySecretRef`, exposed under the provider's **standard** env name — never plaintext in the CRD.

| Provider value | Transport | Endpoint var | Key env |
|---|---|---|---|
| `ollama` | `/api/chat` | `AGENTGATE_OLLAMA_HOST` | none |
| `openai` / `azure` / `runpod` / `vllm` / `tgi` / `openai-compatible` | `/v1/chat/completions` | `AGENTGATE_OPENAI_BASE_URL` or `llm.endpoint` | `<PROVIDER>_API_KEY` (→ `OPENAI_API_KEY` fallback) |
| `anthropic` | Messages API | `llm.endpoint` (opt) | `ANTHROPIC_API_KEY` |
| `gemini` | `generateContent` | `llm.endpoint` (opt) | `GEMINI_API_KEY` |
| `bedrock` | boto3 Converse | AWS SDK | `AWS_*` (extra: `agentgate[bedrock]`) |

RunPod serverless example: `llm.provider: runpod`, `llm.endpoint:
https://api.runpod.ai/v2/<id>/openai/v1`, `apiKeySecretRef.envName: RUNPOD_API_KEY`.

---

## 5. Governance (DriftWatch interop)

`govern.proxyType`:
- **`none`** (default) — standalone; tools called directly; no governance.
- **`driftwatch`** — at startup AgentGate (a) **pushes** the contract once to `govern.register`
  (under `ref = app id`), and (b) **binds** every agent to the DriftWatch proxy at `govern.endpoint`,
  so every tool call is governed. Every call carries `_meta.app = app id` for routing.

The app id is single-sourced: `govern.app` → else `AGENTGATE_APP` (chart `app`, default release
name). Keep it **stable and unique per app** — DriftWatch blocks calls whose `_meta.app` matches no
registered contract (`unknown_app`). See DriftWatch's manual for the central multi-app model.

---

## 6. Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/` | — | `{service, agents, coordinator, dynamic}` |
| GET | `/healthz` | — | `{status: ok}` |
| POST | `/run` | `{"goal": "..."}` (**`content-type: application/json`**) | `{coordinator, history, violations}` |

You command the **coordinator** (top of the graph), not each agent.
