# E10 — reconnect-on-session-terminated at the call hop (design)

**Status:** **implemented** (commit `26c93f0`) and validated in-cluster. Closed the last E10 gap:
a *clean, repeatable* cross-server **call-path** e2e. Aggregation (FR-16) + cross-server scoring
(FR-17) were already validated in-cluster; the forward-to-upstream call (which flaked on the
upstream's idle-session termination) now survives via reconnect.

## Problem

In-cluster, against real `kubernetes-mcp-server` upstreams, a *delayed* `tools/call` fails with
`McpError: Session terminated` and never reaches the upstream. Observed:

- `tools/list` (at startup, immediate) is stable — the lifespan long-lived clients fixed that.
- `tools/call` (delayed, after the proxy has been idle) is not — the upstream has meanwhile
  **terminated the idle Streamable-HTTP session** server-side; the next request reuses a
  server-closed session.
- This is **upstream session-lifecycle behavior**, reproducible even single-upstream
  (sequential `Client(url)` opens: attempts 1–2 fail `Session terminated`, attempt 3 OK).
  The upstream also 404s on `DELETE /mcp` (session teardown), confirming non-standard session
  handling.

So the proxy needs to **survive an upstream that silently drops idle sessions**: detect the
session-class failure on forward, re-establish the upstream session, and retry the forward —
**without** re-scoring, and **without** unsafe double-execution.

## Binding decisions

### D1 — Retry only the session class, nothing else
Retry **only** when the forward fails with a session-lifecycle error
(`McpError("Session terminated")`, session-not-found, or a `DELETE /mcp` 404 surfacing as a
closed session). **Never** retry on:
- a DriftWatch verdict (`block`/`drop` → `ToolError`) — that is a decision, not a transport fault;
- a genuine upstream `ToolError` (bad args, tool failure) — retrying won't help and may double-act.

Rationale: retry is a *transport-resilience* mechanism, not an enforcement or correctness one.

### D2 — Score once; retry forwards only
DriftWatch scoring (append to the `DecisionChain`, compute the decision, emit OTel) happens
**exactly once per `tools/call`**, *before* the forward. A session-class failure on forward is a
transport event *after* the governance decision was already made (`forward`). Retry re-runs **only
the upstream forward**, never the scoring. So:
- the chain is appended once (no phantom self-transition from a retried call — the bug we already
  saw when a naive client-side retry double-appended);
- the OTel decision span is single; the retry is annotated on it (D4), not a second decision.

### D3 — Idempotency / destructive-call policy
A session-class failure *usually* means the upstream never executed the tool (the session was
closed before/at request dispatch). But "usually" is not "always", so:
- **Read-class tools** (risk tier low; `get`/`list`/`watch`-shaped): retry is safe → **on**.
- **Write/destructive tools** (risk tier high; `delete`/`create`/`apply`/`scale`-shaped): retry is
  **off by default** — a second execution could double-act (delete twice, etc.). A destructive
  call that hits a session fault returns the error to the agent (which, per E9, does not retry-storm).
- **As implemented:** the guard is `_looks_destructive(tool, risk, destructive_risk)`. It uses the
  detector's `ToolCall.risk` tier when available, **but** at the MCP hop the adapter usually has no
  catalog so `risk == 0` — relying on risk alone would silently make destructive calls
  retry-eligible. So it **falls back to a conservative tool-name heuristic**: a name containing any
  of `delete/remove/destroy/drain/evict/create/apply/patch/update/replace/scale/restart/exec/...`
  is treated as destructive and not retried. Read-shaped names (`list/get/watch/describe/...`) stay
  retry-eligible. Opt-in override: `retry_destructive=True`.

This keeps the governance project's safety posture: **we never turn a transport retry into an
unintended second destructive action** — even when the upstream's tool risk is unknown.

### D4 — Observable retries *(roadmap — not yet wired in v1alpha1)*
Intended: annotate the retry on the *same* decision span (score-once), so the audit trail shows one
governed decision plus its transport recovery — `gen_ai.agent.gate.forward.retried/retry_count/
retry_reason`; no new event; no `drift.*` namespace (C1). **Status:** the retry is bounded and
score-once today, but these OTel attributes are **not emitted yet** — roadmap.

### D5 — Bounded
At most `max_retries` (**default `2`, hard-coded in `DriftMiddleware` for v1alpha1**) re-establish+
retry attempts, with a short backoff. If still failing, surface the original session error to the
caller (fail toward the declared `failurePolicy`). **Config:** `max_retries` / `retry_destructive`
are constructor knobs with safe defaults; **Helm `mcpProxy.reconnect.*` values are roadmap** (not
exposed yet) — the safe defaults apply.

## Architecture

The forward path today: `on_call_tool` → (score) → `call_next` → FastMCP mount → upstream
long-lived client (from the lifespan). The flake is inside `call_next` → that client's session.

**Chosen: Option B** (probe-confirmed in venv, then implemented). Two options were considered:

- **Option A — reconnect the lifespan client.** Share the per-upstream client/proxy handles with
  the middleware; on a session-class failure, close+reopen that upstream's `Client`, re-mount its
  proxy, and retry `call_next`. Keeps one routing path (mount). Cost: runtime remount semantics in
  FastMCP need verifying.
- **Option B — fresh-client call routing.** Keep the lifespan clients for `tools/list` only; in
  the middleware, on `forward`, resolve `<server>_<tool>` → (upstream, tool) and call the upstream
  with a **fresh** `Client(url)` per call (open→call→close), retrying on the session class. Bypasses
  the flaky reused session entirely; simpler reconnect, at the cost of a connection per call
  (acceptable — these are governance hops, not a hot loop; latency budget is per-call scoring, not
  connection setup). Routing/namespacing we already own (`_validate_server_names`).

**Why B**: it sidesteps the reused-session class of bug rather than fighting FastMCP's mount
session reuse, and reconnect becomes "open a new client and try again" — trivially correct and
score-once-friendly. A venv probe confirmed a fresh per-call `Client(url)` to
`kubernetes-mcp-server` is stable under idle gaps; `tools/list` continues to serve from the
lifespan long-lived clients (mount). Implemented in `DriftMiddleware._forward_fresh`.

## Acceptance (what "done" added to E10)
- [x] Clean, repeatable in-cluster within-baseline **cross-server forward** to two real upstreams,
      returning data (after a 3 s idle gap, no client-side retry). *(in-cluster TC-F-38)*
- [x] A session-class fault is transparently recovered (read-class), the call succeeds, scoring
      ran once. *(unit: reconnect-retry + score-once tests; OTel retry attributes are roadmap, D4)*
- [x] A destructive call under a session fault is **not** double-executed (retry off by default,
      tool-name heuristic when risk is unknown). *(unit: destructive-not-retried test)*
- [x] Single-upstream path (E7/E8/E9) unchanged and green. *(call_next when no upstream map)*
- [x] Unit tests: session-class retry (read) succeeds; non-session error not retried; destructive
      not retried by default; score-once (chain appended once).

## Out of scope
- Fixing `kubernetes-mcp-server` session lifecycle (upstream). We make the *proxy* resilient.
- A general MCP gateway / connection pool. This is targeted resilience for session-dropping upstreams.
