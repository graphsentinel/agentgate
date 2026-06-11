# E7 — MCP-proxy enforcement design (path B, real Kagent)

> **This is Option A — the recommended REFERENCE implementation path.** See
> `e7-architecture-options.md` for the A-vs-B comparison: A is the reference because it holds
> per-session decision-chain state natively at the MCP hop (DriftWatch's chain-aware thesis).
> Option B (agentgateway + ext_authz) is a **production deployment pattern**, gated on a
> spike proving agentgateway forwards the raw `tools/call` body plus a stable
> session/agent/task correlation key.

**Status: design only.** This doc is the plan so the implementation, when it lands, is
mechanical and grounded in what already exists.

## Don't hand-roll MCP transport — use an MCP library (FastMCP)

The earlier draft of this doc proposed writing JSON-RPC framing, `tools/list` passthrough,
and upstream forwarding by hand (`interceptor/mcp.py` + `mcp_proxy.py`). **That was wrong** —
the fragile part (the protocol) is exactly what a maintained library should own. DriftWatch
is Python, so build on an MCP library rather than the wire.

**Package choice (verified against fastmcp 3.3.1 / mcp 1.27.2).** Two ecosystems exist and
must not be conflated: the **official `mcp` SDK** (PyPI `mcp`, `modelcontextprotocol/python-sdk`)
and the separate ergonomic **`fastmcp`** package (PyPI `fastmcp`), which is where the proxy +
middleware ergonomics live; `fastmcp` depends on `mcp`. We target **`fastmcp`** via the opt-in
`mcp` extra (`pip install -e '.[mcp]'`). API names below are **pinned to the installed
fastmcp 3.3.1**, not indicative:
- proxy: `fastmcp.server.create_proxy(backend) -> FastMCP` — the non-deprecated form in
  3.3.1 (`FastMCP.as_proxy` still exists but emits a deprecation warning pointing here);
  `backend` accepts the upstream URL / `Client` / transport,
- middleware: `from fastmcp.server.middleware import Middleware`, hook
  `async def on_call_tool(self, context, call_next) -> ToolResult`,
- call fields: `context.message.name`, `context.message.arguments`,
- session key: `context.fastmcp_context.session_id`,
- block: `from fastmcp.exceptions import ToolError` (note: `ToolError` is in `fastmcp`, not in
  the base `mcp` package),
- result type: `fastmcp.tools.tool.ToolResult`.
Re-verify these if the pinned version changes.

What the library gives us:

- **Streamable HTTP** transport (the recommended prod transport; SSE is deprecated).
- A **proxy/mediator** primitive — `fastmcp.server.create_proxy(backend)` (fastmcp 3.3.1) — so DriftWatch
  can be an MCP **server** to Kagent and an MCP **client** to the upstream ToolServer at once,
  with `tools/list`, session lifecycle, streaming, and error mapping handled by the library.
- **Middleware** with an `on_call_tool(self, context, call_next)`-style hook — the seam to
  insert DriftWatch scoring before a call is forwarded upstream.

So E7 becomes: a library-provided MCP proxy + one middleware class that calls the existing
`Interceptor`. No hand-written transport. The detection core is unchanged —
`Interceptor.handle(dict) -> Verdict`, scoring against the operator-reconciled baseline
(FR-10 shared store).

## The integration point (verified understanding)

Real Kagent is **Helm-installed and controller-managed**: an `Agent` CRD → a controller-
created agent pod. Tool calls leave the agent pod over **MCP Streamable HTTP** to separate
**MCP ToolServer** pods. DriftWatch's enforcement seam is the **MCP tool-call hop**:
DriftWatch sits as an MCP proxy/mediator between Kagent and the real ToolServer, registered
via Kagent's `RemoteMCPServer`.

```
Kagent agent pod ──MCP──> DriftWatch (FastMCP proxy + middleware) ──MCP──> real MCP ToolServer
                          on_call_tool: Interceptor.handle()
                          forward => upstream result
                          block   => ToolError "blocked …" (upstream NOT called)
                          drop    => ToolError "dropped …" (upstream NOT called)
```

Because DriftWatch terminates the MCP session as a server, it sees the whole session and can
hold **per-session decision-chain state** itself — the chain-aware thesis, native at the hop.

## What to build

### T-E7.1 — MCP proxy/mediator via the library (no hand-rolled JSON-RPC)

A proxy app built with the MCP library: an MCP server (facing Kagent) backed by an MCP
client to the upstream ToolServer — use `fastmcp.server.create_proxy(backend)` (fastmcp 3.3.1). The upstream URL comes from config
(`DRIFTWATCH_UPSTREAM_MCP`). `tools/list` and non-tool methods are handled by the proxy
automatically (passthrough) — DriftWatch only governs `tools/call`.

### T-E7.2 — DriftWatch enforcement middleware (`on_call_tool`)

A `Middleware` subclass whose `on_call_tool(self, context, call_next)`:
1. reads `context.message.name` (tool) + `context.message.arguments` (dict),
2. maps them to the dict the existing `RuntimeAdapter` eats
   (`{"tool", "namespace"<-args.namespace/scope, "args"}`) — a tiny pure helper, the only
   bit worth unit-testing in isolation,
3. `verdict = interceptor.handle(that_dict)` against the per-session chain (T-E7.3),
4. map the verdict to an MCP outcome:
   - **forward** (within baseline / log / shadow) → `return await call_next(context)` (the
     call proceeds to the upstream ToolServer and its real result is returned);
   - **block** → `raise ToolError(...)` with the drift reason + score, so the call never
     reaches the upstream and the agent sees *why*;
   - **drop** → **do NOT call `call_next`** (the upstream is never invoked); at the MCP hop,
     surface drop as a `ToolError` too — message `"dropped …"` (vs block's `"blocked …"`) —
     **not** a synthetic success. *(Decision made during implementation, superseding the
     earlier "synthetic empty result" plan.)* Reason: a fake success result must satisfy the
     upstream tool's **output schema** (e.g. `{"result": ...}`), which the proxy does not
     know — returning one risks schema-validation failures or a fabricated value the agent
     trusts. An MCP error is schema-agnostic and unambiguous; forward stays the only success
     path, so drop never collapses into log/allow. Drop remains a silent 200/empty **only on
     the sidecar/HTTP hop**, where there is no upstream output schema to satisfy.
    **Drop behavior must still be validated with real Kagent** — some agents may retry on a
    tool error, so confirm the `"dropped …"` error doesn't trigger a retry loop; this is a
    T-E7.5 (real-Kagent) acceptance check.

The chain is still updated on drop (the call was observed), so sequence state stays correct
for the next call. Reuse the same engine wiring as the sidecar (shared baseline + policy from
the FR-10 env + shared-store path); see T-E7.3 for what is shared vs per-session.

### T-E7.3 — Chain correlation (per session/agent/task)

DriftWatch scores ordered **chains**, not isolated calls, so calls must accumulate into the
right chain, keyed per caller so two concurrent agents don't share one chain.

**Correlation key (prefer, then fall back).** The MCP session id is the natural key, but it
is not guaranteed in every middleware phase (it can be absent/None, e.g. during init), so
don't assume it: **prefer the MCP `session_id`; fall back to a client id / HTTP headers /
an explicit task header; if no stable key can be derived, fail closed (or run a documented
degraded per-call mode), never silently merge unrelated callers into one chain.** This is
the design item the consultant flagged — A still has the advantage that the key is available
in-process at the hop, whereas Option B depends on agentgateway forwarding one.

**What is shared vs per-session.** Do NOT clone the whole engine per session. Share one
**baseline store**, one **policy**, and one **emitter** across all sessions (read-mostly);
keep only the **adapter / chain state** per session (a small per-key object that accumulates
that caller's `DecisionChain`). So scoring reads the shared baseline but appends to the
caller's own chain — cheap, and correct under concurrency.

### T-E7.4 — Unit tests with a fake upstream MCP server

Stand up an in-memory/fake upstream MCP server (the SDK supports in-memory client/server
wiring) and drive the proxy:
- within-baseline `tools/call` → forwarded, upstream result returned (TC-F-16),
- drifting `tools/call` → `ToolError`, upstream **never invoked** (TC-F-17),
- the pure name/args→engine-dict mapping (canned inputs, no network).
No real Kagent/cluster needed for these.

### T-E7.5 — Real Kagent `RemoteMCPServer` e2e (later, gated)

- Helm: optional `mcpProxy.enabled` Deployment+Service running the proxy, mounting the
  baseline store read-only + policy env (like the sidecar).
- A `RemoteMCPServer` example pointing real Kagent at the proxy Service; document the
  real-Kagent install (path B in `examples/k3d-cluster-demo/README.md`) + model-provider
  secret + an upstream ToolServer.
- e2e script (like `fr10-e2e.sh`): within-baseline task → tool executes; drift task → MCP
  error, ToolServer never reached.

## Dependencies

Add `mcp` (official SDK) — and `fastmcp` if we use the FastMCP layer — under a new optional
extra (e.g. `mcp = ["mcp>=...", "fastmcp>=..."]`), folded into `all`, so the proxy is opt-in
and the core/library install stays lean. Pin versions when implementing.

## Tests

- **TC-F-16** — within-baseline `tools/call` forwarded to the upstream (unit: fake upstream;
  e2e: real Kagent).
- **TC-F-17** — drifting `tools/call` → MCP error, upstream never called (unit + e2e).
- Mapping unit test: name/args → engine dict, canned inputs, no network.

## Gherkin

```gherkin
Feature: MCP-proxy enforcement against the reconciled baseline (E7, path B)

  Scenario: A within-baseline tools/call is forwarded               # TC-F-16
    Given a baseline the operator reconciled for task "investigate_latency"
    And real Kagent pointed at DriftWatch as a RemoteMCPServer
    When Kagent issues a tools/call within that baseline
    Then DriftWatch forwards it to the upstream MCP ToolServer and returns the result

  Scenario: A drifting tools/call is blocked at the MCP hop          # TC-F-17
    Given the same reconciled baseline
    When Kagent issues a tools/call that drifts (unexpected tool/scope/sequence)
    Then DriftWatch returns an MCP error and never calls the upstream ToolServer
```

## Boundaries (what E7 is NOT)

- Not new detection logic — same `Interceptor.handle` + `score_chain` + baseline.
- Not hand-rolled transport — the MCP library (FastMCP) owns JSON-RPC, `tools/list`,
  streaming, sessions, error mapping.
- Not a replacement for path A's demo — the in-process `make demo` + stand-in sidecar stay
  the deterministic, dependency-free demo/fallback. E7 is the *real-Kagent* path.
- Not operator-embedded — the proxy is a data-plane workload like the sidecar; the operator
  still only reconciles + writes the baseline.

## Sequencing reminder

Across the remaining roadmap: **FR-9 `runner.py`** (live model panel for consensus) →
**webhook sidecar injector** → **E7 (this doc)**. Within E7, the near-term, cluster-free
work is T-E7.1–T-E7.4 (MCP-library proxy + middleware + chain keying + fake-upstream unit tests);
T-E7.5 (real Kagent e2e) is gated on the cluster pieces. The Option-B spike
(`e7-architecture-options.md`) runs in parallel and decides whether the gateway pattern can
also be a primary production topology.
