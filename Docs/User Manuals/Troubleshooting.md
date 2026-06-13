# AgentGate — Troubleshooting

Concepts and concrete fixes for the problems you are most likely to hit. Pairs with
**[Installation.md](Installation.md)** and **[Configuration.md](Configuration.md)**.

## How to read AgentGate's behavior first
- **Stub vs live mode.** With no `AGENTGATE_LLM_PROVIDER`, agents run a deterministic stub: the `/run`
  `history` has agents + tools but **no `output` field**. That is *not* a bug — it proves wiring
  without a model. Set the LLM provider to go live.
- **Static vs dynamic.** `dynamic:false` can never produce an undeclared hand-off (it's not in the
  generated graph). If you expect runtime routing, you need `dynamic:true`.
- **The coordinator is the entry.** `/run` drives the top of the graph, not individual agents.

---

## Install / run

### `address already in use` on :8000, or `/run` returns a weird `400`
Port 8000 is taken (Harbor, another container, a prior `agentgate` run). Two symptoms together:
the container won't bind, and your `curl localhost:8000` hits *the other* service (often a non-JSON
`400` → `jq: parse error: Invalid numeric literal`).
**Fix:** map a free host port and curl that: `-p 8001:8000` → `curl localhost:8001`. Check the
occupant with `ss -ltnp | grep :8000` / `podman ps`.

### `/run` returns 422 / "field required"
The body wasn't parsed as JSON. `/run` uses a JSON `Body(...)`. **Always send**
`-H 'content-type: application/json'`; `curl -d` alone sends form-encoded.

### Nothing returned from `curl` (empty)
No listener on that port — the container isn't running (or you cleaned it up). Start it first, then
curl: `podman run -d --name agentgate -p 8001:8000 …` then `sleep 3` then curl. `curl -s` silently
swallows connection-refused, so an empty result usually means "nothing is listening."

### Image won't pull
The package may still be private. Either `podman login ghcr.io` with a PAT (`read:packages`), or have
the owner flip `agentgate` + `charts/agentgate` to **Public** (one-time).

---

## Org / generation

### `duplicate tool` / `duplicate agent` / `delegation from unknown agent`
The AgenticArchitecture failed validation at load — the pod won't go Ready (fail-fast by design).
Fix the spec: unique tool names, unique agent names, every `delegations.from/to` must name a declared
agent.

### A cyclic or scope-escalating graph fails to load
`validate_for_generation` rejects a cyclic delegation graph or a hand-off that widens scope at
reconcile time (not just codegen). Break the cycle or keep child scope ⊆ parent scope.

### An agent calls a tool it shouldn't / can't use a tool you granted
Tools are **least-privilege and creation-driven**: the model is only ever offered the agent's bound
`tools` (+ resolved `mcpServers`). If a tool is missing, add it to that agent's `tools` or a bound
`mcpServers` backend (mind `allow`/`deny` globs). If an unbound tool is requested, it's refused by
design.

---

## LLM

### Agents produce no `output` even though I set a model
You set `model` but not `AGENTGATE_LLM_PROVIDER` (or `llm.provider`). Both are required to leave stub
mode. Set the provider env.

### `unknown LLM provider '…'`
`AGENTGATE_LLM_PROVIDER` must be one of: `ollama`, `openai`/`azure`/`runpod`/`vllm`/`tgi`/
`openai-compatible`, `anthropic`, `gemini`, `bedrock`.

### 401/403 from the model API
The key isn't reaching the container under the right name. Keys use **standard** names
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `RUNPOD_API_KEY`) — not `AGENTGATE_`-prefixed.
Set `llm.apiKeySecretRef.{name,key,envName}` so the chart injects it via `secretKeyRef`.

### `bedrock provider needs boto3`
Install the extra: `pip install agentgate[bedrock]` (or use the image, which already includes it only
if built with that extra). Provide `AWS_REGION` + credentials.

### Agent can't reach Ollama on the host from inside k3d
Pods can't resolve the host. Register the CoreDNS alias and use it:
`./examples/.../register-host-alias.sh <cluster>` then
`AGENTGATE_OLLAMA_HOST=http://host.k3d.internal:11434`. Re-run the script if the host DHCP IP changes.
From `podman run`, use `http://host.containers.internal:11434` instead.

### Timeouts on long tool loops
Raise `AGENTGATE_LLM_TIMEOUT` (per-call seconds) and/or `AGENTGATE_TOOL_ITERS` (max tool iterations).

---

## External MCP tools (`mcpServers`)

### Backend tools don't appear / agent has fewer tools than expected
Best-effort by default: an unreachable MCP server registers **nothing** and the app still starts
(standalone-safe). For prod, set `AGENTGATE_MCP_STRICT=true` so an unreachable backend **fails
startup** (pod not Ready) instead of silently serving a tool-less agent. Also check `allow`/`deny`
globs aren't filtering everything out.

### `'_meta' is a reserved argument key`
A real tool argument is named `_meta`, which collides with the cross-check metadata channel. Rename
the tool argument.

---

## Governance (DriftWatch interop)

### Contract push to DriftWatch failed (warning in logs)
With `govern.proxyType: driftwatch`, a failed push logs a WARNING but the tool path still routes to
the proxy. DriftWatch has no declared contract until the next successful push — so until then, in a
central DriftWatch with strict routing, this app's calls may be blocked as `unknown_app`. Ensure
`govern.register` is reachable at startup, or restart the pod to re-push.

### DriftWatch blocks everything with `unknown_app`
The app's `_meta.app` must match a contract registered in DriftWatch. Causes: the contract push
hasn't landed (DriftWatch was down at startup), or the `app` id differs between the push `ref` and
the runtime. Keep `app` **stable and unique**; it is single-sourced (`govern.app` → `AGENTGATE_APP`).

### Undeclared hand-offs in dynamic mode are dropped
Expected. In `dynamic:true`, a pick outside the declared graph (novel edge / cycle / scope
escalation) is recorded in `state.violations` and, with `AGENTGATE_DELEGATION_ACTION=block`
(default), the hand-off is dropped and routed to END. Add the edge to `canDelegateTo` if it's
legitimate.

---

## Kubernetes

### Pod not Ready
Check `kubectl logs` — most often a validation failure (bad org), a strict MCP backend unreachable,
or a missing LLM Secret. The liveness/readiness probe hits `/healthz`.

### Changing the org doesn't take effect
Edit `values.org` and `helm upgrade`. A `checksum/org` annotation rolls the pod so the new ConfigMap
is read. If you edited the ConfigMap directly, the pod won't auto-roll — prefer `helm upgrade`.
