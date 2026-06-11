# E7 architecture — two options for the real-Kagent enforcement hop

**Design only, no code.** Before implementing E7 we compare two ways to put DriftWatch on
the live MCP tool-call path for real, Helm-installed Kagent. This supersedes the single
approach assumed in `e7-mcp-proxy-design.md` (Option A below) by adding the gateway/ext_authz
approach (Option B) and a recommendation.

Both reuse the detection core unchanged: `Interceptor.handle(dict) -> Verdict`, scoring a
decision chain against the operator-reconciled baseline (FR-10 shared store). The question
is only **who owns the MCP transport** and **how DriftWatch is invoked** — and, decisively,
**where DriftWatch's stateful decision-chain model lives** (DriftWatch's core thesis is
chain-aware governance, not per-call authorization).

---

## Option A — DriftWatch is its own MCP proxy

```
Kagent agent pod ──MCP──> DriftWatch MCP proxy ──MCP──> real MCP ToolServer
                          (speaks JSON-RPC, scores tools/call,
                           forwards survivors, blocks drift)
```

DriftWatch registers as Kagent's `RemoteMCPServer` and acts as an MCP proxy/mediator — but
it does **not** hand-roll the protocol. It builds on an MCP library (FastMCP): proxy support
(`fastmcp.server.create_proxy(...)` in fastmcp 3.3.1) for the server↔upstream proxy,
and an `on_call_tool`-style middleware hook to score each call before forwarding. See
`e7-mcp-proxy-design.md` for the detailed plan and package/version caveats.

**What we'd build:** a FastMCP proxy + one enforcement middleware class
(`on_call_tool` → `Interceptor.handle()` → forward or `ToolError`) + a tiny pure
name/args→engine-dict mapping + fake-upstream unit tests. No hand-written JSON-RPC/transport.

**Pros**
- No dependency on agentgateway — DriftWatch is self-contained.
- **DriftWatch sees the whole MCP session**, so per-session decision-chain state lives
  exactly where the thesis needs it — at the hop, in DriftWatch. The library exposes the
  session in middleware (prefer `session_id`, fall back to client id / headers / a task
  header), so chain correlation is available in-process — not something a gateway must be
  coaxed into forwarding. This is the strongest reason to keep A as the reference path.
- Transport is the library's job (JSON-RPC, `tools/list`, Streamable HTTP, sessions, error
  mapping) — we own only the scoring middleware.

**Cons**
- Adds an MCP SDK dependency (opt-in extra) and a real MCP runtime to operate.
- A full proxy is still not trivial even with the SDK — session lifecycle, streaming
  responses, and upstream reconnect need care; but this is library-supported, not
  hand-rolled, which is the key difference from the original draft.

---

## Option B — agentgateway in front, DriftWatch as external authorization (your proposal)

```
Kagent agent pod ──MCP──> agentgateway ──MCP──> real MCP ToolServer
                              │
                              └── ext_authz (per tools/call) ──> DriftWatch
                                  HTTP 200 = allow, non-2xx = deny
```

agentgateway is an AI-native MCP/A2A gateway that can sit in front of Kagent tool traffic;
it supports **External Authorization** — for each request it calls an external service and
**allows on HTTP 2xx, denies otherwise** (API-compatible with the Envoy ext_authz model,
with an MCP-aware authz path that evaluates `call_tools` invocations). *(The exact
deployment relationship to Kagent — waypoint/ingress/mesh — should be confirmed against
current docs rather than asserted here.)*

**Partial fit:** DriftWatch's *existing* `/v1/tool-call` endpoint already returns **200 for
forward and 403 for block**, so the allow/deny *semantics* line up. But the ext_authz
*envelope* (request body shape, headers, caller identity, timeout semantics) will differ
from DriftWatch's current request shape, so this is a **thin adapter**, not a free match.

**What we'd build:** a thin request-adapter so `/v1/tool-call` accepts the ext_authz body
agentgateway sends (tool name + arguments, identity headers), plus deploy wiring (an
agentgateway config pointing ext_authz at the DriftWatch Service) + an example. The drift
logic, baseline, and FR-10 handoff are reused as-is.

**Pros**
- **Much less new code** — no MCP transport in DriftWatch; the mature proxy handles framing,
  streaming, sessions, `tools/list`.
- A natural **production deployment pattern**: govern Kagent tool calls at the gateway the
  platform already runs, instead of inserting a bespoke proxy.
- Consistent with the earlier "don't open a separate fragile surface" calls (e.g. deferring
  the admission webhook).
- Reuses the already-tested 200/403 decision path → most of E7 becomes config + an adapter.

**Cons**
- Adds a runtime dependency on agentgateway (must be installed in the path-B cluster).
- **Chain-state is the open risk** (below). ext_authz is per single tool call; DriftWatch's
  whole point is ordered-chain drift. B only preserves the thesis *if* agentgateway forwards
  a stable correlation key — unproven until a spike confirms it.

---

## The chain-state nuance (applies to both, decisive for B)

DriftWatch scores a **decision chain** (ordered sequence of tool calls per task), not just
one isolated tool. In a per-call authorization hop, each call arrives separately, so
DriftWatch must accumulate the chain across calls keyed by something stable (session /
agent_id / task). The current `Interceptor` already appends each observed call to a chain on
its adapter — so single-process accumulation exists; what E7 needs is to **key chains by the
caller** so two concurrent agents don't share one chain.

- **Option A:** DriftWatch sees the whole MCP session, so it can hold per-session chain
  state itself. The library exposes the session in `on_call_tool` middleware — prefer
  `session_id`, fall back to client id / headers / a task header (it can be absent in some
  phases), keying chains per caller in-process. Shared baseline/policy/emitter, per-session
  adapter/chain only.
- **Option B:** agentgateway must pass a stable correlation id (session/agent) in the
  ext_authz request so DriftWatch can bucket calls into the right chain. This **must be
  confirmed** against agentgateway's ext_authz request contract before B can be primary.
  - If it forwards the raw `tools/call` body **and** a stable session/agent/task key → B
    preserves chain-aware governance and is a strong production pattern.
  - If it does not, the fallback is **per-call features only** (tool, scope, argSchema) at
    the hop, with sequence/chain drift relegated to the trace/postmortem. **This is a
    degraded mode, not sufficient for E7 success** — it removes DriftWatch's most
    distinctive capability (sequence/chain drift) from runtime enforcement, so it cannot be
    accepted as the E7 outcome; it would only be a stopgap while the correlation gap is
    closed.

---

## Recommendation

**Keep Option A as the reference implementation path for E7**, because it preserves
DriftWatch's stateful decision-chain model directly at the MCP hop — which is the thesis E7
exists to prove. **Treat Option B (agentgateway + ext_authz) as a production deployment
pattern**, gated on a spike proving that agentgateway forwards the raw `tools/call` body
**plus** a stable session/agent/task correlation key. If the spike passes, B becomes the
recommended *production* topology while A remains the reference that demonstrates chain-aware
enforcement end-to-end.

Rationale for not making B primary yet: the choice is a product-thesis decision, not a
lines-of-code decision. B is less code, but until the correlation key is proven it cannot be
shown to preserve chain-aware drift — and that is precisely what makes DriftWatch more than a
per-call tool allowlist. We do not let "minimal code" pick the architecture for the core
capability.

**Sequencing (revised):**
1. **Spike — agentgateway ext_authz contract** (doc/spec, no cluster): does the request body
   carry the MCP tool name + arguments, and is there a stable session/agent/task field for
   chain correlation? This spike is the decision gate for B's status.
2. **Reference path (A) core** — the pure name/args → engine-dict mapping (`to_engine_call`),
   pure and unit-tested, no cluster. Useful to *both* options (B can reuse it if agentgateway
   forwards the raw body).
3. **A proxy shell + chain keying** — the MCP-library proxy (`fastmcp.server.create_proxy`) plus the
   `on_call_tool` enforcement middleware and per-session chain state; unit-tested with a fake
   upstream MCP server. (The library owns transport — we do not write JSON-RPC.)
4. **Live e2e** — A against a real Kagent + ToolServer; and, if the step-1 spike passed,
   a B (agentgateway ext_authz) wiring + e2e. Both gated on the real cluster pieces being
   available.

Until those cluster pieces exist, the in-process `make demo` + path-A stand-in remain the
deterministic demo, exactly as today.

---

## What carries across both options

The pure name/args → engine-dict mapping is reusable in B too if agentgateway forwards the
raw `tools/call` body to ext_authz. So that small mapping helper is not wasted regardless of
which topology ships — it is the shared seam between the reference proxy (A, where it sits in
the `on_call_tool` middleware) and the gateway pattern (B, where it adapts the ext_authz
body). The transport itself differs: A delegates it to the MCP SDK, B delegates it to
agentgateway — in neither option do we hand-roll JSON-RPC.
