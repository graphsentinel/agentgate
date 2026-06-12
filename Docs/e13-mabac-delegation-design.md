# E13 — Intra-framework multi-agent orchestration as code (design)

**Status:** design (pre-implementation). E13 turns one declared file into a running, governed
multi-agent system **inside a single framework, in a single pod**. Four stages over the E11/E12
contract:

```
Declare ─▶ Generate ─▶ Execute ─▶ Govern
(YAML)     (codegen)   (1 pod)    (creation-driven + observability-driven)
```

- **Declare** — all agents + wiring in one framework-agnostic YAML: per-agent `instructions` /
  `model` / `tools` / `scope`, the `delegations` graph (who may hand off to whom), and `rules`
  (forbidden orderings). This is the E11 `AgenticArchitecture` contract; E13 adds `instructions` /
  `model`.
- **Generate** — a per-framework generator turns the YAML into runnable code (LangGraph / AutoGen /
  CrewAI): `delegations` → framework edges/transitions, `instructions` → agent prompt, `tools` →
  bound tools, `rules` → the forbidden edge is **not generated**.
- **Execute** — all agents run as nodes in **one process / one pod**. The user submits a goal to a
  single entry point — the **coordinator** agent (top of the graph) — which delegates down the
  declared graph and returns the result. *You command the coordinator, not each agent.*
- **Govern** — two modes: **creation-driven** (rules baked into generated code → a forbidden
  hand-off is impossible) and **observability-driven** (watch runtime hand-offs, score them against
  the contract: novel-edge / up-edge / **cycle (DAG)** / scope-escalation / Aegis).

**E13 is intra-framework**: one framework, one pod, agents as in-process nodes. Cross-framework /
cross-pod / A2A is **E14**. The sections below detail Execute (§A) and the Govern modes (§1–§7);
Declare/Generate reuse the E11 contract + agentic-lab codegen.

## §A. Execution model — how the agents run and who you command

After `Generate`, you have **one deployable** (a LangGraph/AutoGen/CrewAI app), packaged as **one
pod**. There is **one entry point**, and it is the **coordinator** agent:

```
user ──"goal"──▶ POST /run  (or CLI)         ── one pod ──────────────┐
                      ▼                                                 │
                 coordinator (planner)   ← the only thing you command   │
                      │  delegates per the declared graph               │
                      ├──▶ coder                                        │
                      │       └──▶ reviewer                             │
                      ◀── results bubble up                             │
                      ▼                                                 │
                 final answer ──────────────────────────────────────────┘
```

- **How agents come alive:** the generated app *is* the program; when the pod starts, the framework
  runtime loads every agent as an in-process node. You do **not** start agents individually.
- **How you command them:** one request (HTTP `POST /run {"goal": …}` or a CLI) to the **coordinator**
  — not to each agent. The coordinator is just the graph's entry node (the pyramid's strategic agent).
- **How work flows:** the coordinator hands off down the declared `delegations` graph; each agent does
  its `instructions` with its `tools`; results bubble back up; the coordinator returns the answer.
- **Where governance sits:** *creation-driven* rules are already in this code (a forbidden hand-off
  was never generated); *observability-driven* checks ride the framework's callbacks/telemetry inside
  the pod (a sidecar/hook), scoring each hand-off against the contract.

> **The hard part of the observability-driven mode is observation.** Where a hand-off physically
> happens decides whether a governor can see it (see §3) — for intra-framework/in-pod, that means a
> framework callback/hook, not an external proxy.

## §B. Declare + Generate — the creation-driven path

This is the primary path for the single-pod scenario: one YAML → one framework app, with the rules
baked in so a violation is impossible *by construction* (no runtime check needed for the structural
rules).

### Declare — one YAML, every field

```yaml
apiVersion: driftwatch.graphsentinel.org/v1alpha1
kind: AgenticArchitecture
metadata: { name: dev-org, namespace: agents }
spec:
  agents:
    - name: planner
      role: "Break a goal into ordered steps"
      model: gpt-4o                         # E13 adds
      instructions: |                       # E13 adds — the agent's actual job (prompt)
        You are a planner. Turn the goal into ordered steps. Do not write code.
        Hand each step to coder.
      tools:  [search]                      # E11 — what it may use
      scope:  ["project:acme"]              # E11 — where it may act
    - name: coder
      role: "Implement a step"
      model: gpt-4o
      instructions: "You implement the step coder is given, then hand off to reviewer."
      tools:  [write_file, run_tests]
    - name: reviewer
      role: "Review code"
      model: gpt-4o
      instructions: "Review for bugs/security; report issues."
      tools:  [read_file]
  delegations:                              # E11/E13 — the DAG (who may hand off to whom)
    - { from: planner, to: coder }
    - { from: coder,   to: reviewer }
  rules:                                    # E12 — forbidden orderings
    - deny: [reviewer, coder]               # reviewer may never delegate back to coder
      reason: "review must not loop back into authoring"
```

`role`/`model`/`instructions` are E13 additions to the agent record; `tools`/`scope`/`delegations`
are E11; `rules` are E12. **The coordinator** is the graph's entry agent (here `planner` — no
incoming edge); §A shows how it is commanded.

### Generate — per-framework, same YAML

| YAML field | LangGraph | AutoGen | CrewAI |
|---|---|---|---|
| `agents[].instructions` | node's system prompt | `ConversableAgent(system_message=…)` | `Agent(backstory/goal=…)` |
| `agents[].model` | node's LLM | agent `llm_config` | agent `llm` |
| `agents[].tools` | tools bound to node | agent tools | agent tools |
| `delegations` | `graph.add_edge(from,to)` | `allowed_speaker_transitions` | task/agent hierarchy |
| `rules` (deny) | the edge is **not added** | the transition is **not allowed** | the hand-off is **not wired** |
| coordinator (entry) | `set_entry_point(...)` | GroupChat manager seat | the kickoff agent |

### Creation-driven governance (why this is strong)

Because the forbidden hand-off is **never generated**, the running app *cannot* perform it — the
structural rules are enforced by absence, not by a runtime gate. Two build-time guards complete it:
- **DAG validation** — the declared `delegations` must be acyclic (§1); a cyclic graph fails to
  generate.
- **scope monotonicity** — a child's `scope` must be ⊆ its delegator's; a violation fails to
  generate.

What creation-driven **cannot** catch is *runtime* drift an LLM might improvise *within* an allowed
edge (e.g. a tool argument, or a dynamic re-entry that forms a cycle at run time). That residue is
exactly what the **observability-driven** mode (§3–§4) covers — hence both modes.

### External tools via MCP servers (dynamic binding)

`tools` are resolved from a registry. Beyond in-process tools (e.g. the built-in `calculator`), an
org can pull tools from **real MCP servers** declared on the contract — so agents call live tools
(e.g. a Kubernetes MCP server) chosen by config, not code:

```yaml
spec:
  mcpServers:
    - { name: k8s, url: http://driftwatch-mcp.driftwatch.svc:8000/mcp }   # see "proxy vs direct"
  agents:
    - name: ops
      tools: [k8s_pods_list, k8s_pods_get]    # this server's tools (namespaced <server>_<tool>)
```

At startup AgentGate connects to each `mcpServers[].url` (FastMCP client — reusing the E10
multi-upstream client), lists its tools, and registers a **proxy callable** per tool. The existing
**creation-driven binding then applies unchanged**: an agent is only ever offered the MCP tools it
*declares*; a call to any other is refused. Tool names are namespaced `<server>_<tool>` (the E10
convention), so two servers can't collide.

**Proxy vs direct (the governance choice) — `url` decides, code doesn't.** The client is
URL-agnostic because the DriftWatch MCP proxy *also* speaks MCP, so it slots in transparently:

| `mcpServers[].url` points at | AgentGate binding (which tool) | DriftWatch runtime drift (chain/baseline) |
|---|---|---|
| the **MCP server directly** | ✅ creation-driven | ✗ |
| the **DriftWatch chain-aware proxy** (E8/E10) | ✅ creation-driven | ✅ blocked_transition / baseline / declared (E11/E12) |

The two layers are orthogonal and complementary: **binding** governs *which* tool an agent may call
(generation/prompt boundary); the **proxy** governs *how the chain of calls behaves* at run time.
Recommended: point `url` at the proxy → one config line, both layers. Connection failures degrade
gracefully (the server's tools are simply absent — standalone-safe).

**Backend binding (whole-backend, not tool-by-tool).** Beyond listing tools one by one
(`tools: [k8s_pods_list, …]`), an agent can bind a **whole backend** — it then gets *all* of that
server's tools, without enumerating them:

```yaml
agents:
  - name: ops
    mcpServers: [k8s_gov]      # ← this agent gets ALL of k8s_gov's tools; selection is the LLM's,
    # tools: []                #   governance is the backend's (DriftWatch proxy if url → proxy)
```
AgentGate keeps no per-tool list here; it just says "this agent may use this backend." Which tool to
call is the agent LLM's choice; whether that call is governed is the backend's job (proxy → drift +
declared; direct → none). Explicit `tools` and `mcpServers` can coexist (explicit + whole-backend).
This is the *"AgentGate only declares the backend, the backend handles tool selection/governance"*
model.

**Optional allowlist (least-privilege — consultant).** Whole-backend is broad (the agent sees
`pods_delete`/`pods_exec` too). An entry may be a **string** (all tools — the default, vision-
preserving, back-compatible) OR an **object** with `allow`/`deny` to narrow the offered set:

```yaml
agents:
  - name: ops
    mcpServers:
      - { name: k8s, allow: [namespaces_list, "pods_*"] }   # ONLY these (glob ok)
      - { name: tekton, deny: [pipeline_delete] }           # all EXCEPT these
      # plain string still works: `- k8s` == `{ name: k8s }` (all tools)
```
Semantics: matched against the **registered (namespaced) tool names** with glob (`fnmatch`); `allow`
first (empty = all), then `deny` removes. Default (neither) = **all** — `mcpServers: [k8s]` is
unchanged. So least-privilege is **opt-in**: simple use keeps every tool via the governed proxy; a
security-minded org narrows with `allow`/`deny`. Creation-driven holds — a tool excluded by the
allowlist is never offered to the LLM.

**Namespace passthrough (avoid double-prefixing behind the proxy).** AgentGate namespaces imported
tools `<name>_<tool>`. But the DriftWatch proxy *already* namespaces its upstreams (`k8s_pods_list`),
so binding it under `name: k8s` yields `k8s_k8s_pods_list` (double). Fix: `name` stays the **backend
reference** (agents bind via it), and a separate **`namespace: false`** keeps the server's tool names
verbatim (no extra prefix) — use it when `url` points at the proxy (already namespaced); leave the
default `namespace: true` for a direct single server.
```yaml
mcpServers:
  - { name: k8s, url: http://driftwatch-mcp…/mcp, namespace: false }   # proxy → keep its names
  - { name: cal, url: http://calc…/mcp }                               # direct → prefix cal_*
```

**Startup robustness.** Importing a backend's tools must not wedge the pod: the import is best-effort
with a **timeout** — an unreachable/slow server yields no tools (logged) and the app still starts
(declared+baseline still govern). Never block readiness on an external MCP server.

**Chain grouping (so DriftWatch sees a chain, not isolated calls).** For DriftWatch to score a
*chain* (sequence drift, baseline) the calls of one agent-run must arrive as **one chain**, not
independent sessions. The proxy correlates a chain by the **MCP transport session id** (confirmed:
`interceptor/mcp_proxy.py` `_interceptor_for` keys interceptors on `fastmcp_context.session_id`; no
`_meta`/header is read — no stable session → degraded per-call mode). So the mechanism is simply:
**AgentGate opens ONE MCP session per agent-run and reuses it for every tool call** — a per-run
`McpSession`: one event loop, one entered `Client` per url, all calls go through it. Without this,
`_mcp_proxy` opens a fresh `Client`/session per call → the proxy sees isolated per-call chains, so
only per-call checks (unbound/out-of-scope/declared) fire; sequence drift + baseline need the reuse.
No correlation key/`_meta` is needed — session reuse is sufficient.

### Configurable LLM (global + per-agent) and instruction sourcing

The LLM is the agent's *brain* (AgentGate owns it; DriftWatch is LLM-agnostic). Two ergonomic
additions, both back-compatible and intentionally **not** a named-provider registry (yalın: global
default + override only):

**Global + per-agent LLM.** A `spec.llm` sets the org default; an agent overrides it field-by-field:

```yaml
spec:
  llm: { provider: ollama, model: qwen3.5:9b, endpoint: http://host.k3d.internal:11434 }
  agents:
    - { name: planner }                                    # uses the global llm
    - { name: coder, llm: { model: gpt-4o, provider: openai } }   # overrides just these fields
```

Resolution per field (most specific wins → general fallback), so nothing existing breaks:
```
effective.<provider|model|endpoint> =
    agent.llm.<f>  ??  agent.model (shorthand for model)  ??  spec.llm.<f>  ??  env(AGENTGATE_*)
```

**Instruction sourcing — inline *or* config.** Keep authoring prompts inline, or pull them from a
ConfigMap key / mounted file so prompts live outside the code:

```yaml
agents:
  - { name: planner, instructions: "You are a planner…" }                 # inline (today)
  - { name: coder, instructionsFrom: { configMapKeyRef: { name: prompts, key: coder.md } } }
  - { name: reviewer, instructionsFrom: { path: /etc/agentgate/prompts/reviewer.md } }
```
Effective instructions = `instructions` (inline) ?? load(`instructionsFrom`); neither → a default
("You are the <name> agent."). Loaded at startup (server) / build (CLI).

**DriftWatch unaffected** — this is the brain axis; `baseline.sources.models` (consensus seeding)
stays separate on the DriftWatch side.

## 1. What it governs

E1–E12 score one agent's decision chain (which tool, in what order, within what scope). E13 scores
the **agent-to-agent edge**: orchestrator → sub-orchestrator → worker. The violations (FR-11B):

- **bypass / skip-level** — an orchestrator delegates straight to a worker, skipping the declared
  sub-orchestrator;
- **novel edge** — a hand-off `A → D` where `D ∉ A.canDelegateTo`;
- **up-edge** — a lower-tier agent delegates to a higher-tier one (inverted hierarchy);
- **cycle / DAG violation** — a hand-off that closes a loop in the delegation graph (`A → B → … → A`);
- **scope escalation** — the delegated task's scope is not a subset of the delegator's
  (**scope monotonicity**, the invariant agentic-lab enforces at *every* edge).

Plus **Aegis** (FR-12): the delegated authority is *attested, not assumed* — a credential on the
hand-off is verified (Keycloak / local-signer), and clearance is non-increasing.

### The delegation graph is a DAG (directed **acyclic** graph)

Both the *declared* graph and a *runtime* hand-off chain must stay acyclic — a pyramid (centralized)
is acyclic by construction (agentic-lab `docs/topologies/pyramid.md`: delegation flows down, reporting
flows up). E13 treats acyclicity as a first-class invariant, in two places:

- **Declared-graph acyclicity (build/reconcile time).** An `AgenticArchitecture` is hand-writable, so
  its `canDelegateTo` edges could form a cycle (`A → B`, `B → A`) or a self-edge. The contract builder
  / operator **must reject a cyclic delegation graph** (same posture as E12's duplicate-name guard) —
  the declared graph is a DAG or it does not reconcile.
- **Runtime cycle (scoring time).** Even over an acyclic declared graph, a *live* hand-off sequence can
  revisit an agent (`A → B → C → A`). E13 **will track** the active delegation path for a task and gate
  a hand-off that re-enters an agent already on the path — a delegation **cycle** anomaly (the general
  case of the up-edge check, and a classic runaway-delegation / infinite-handoff guard).

**Mesh / peer topologies are *not* DAGs** (peer-to-peer negotiation can be cyclic) — agentic-lab marks
mesh as research, and cross-framework peer hand-offs are **E14**. E13's acyclicity invariant applies to
the **intra-framework hierarchical (pyramid)** case it governs; the mesh/over-graph case is deferred.

## 2. Alignment with agentic-lab (the declare side already exists)

E13 reuses agentic-lab's model 1:1 — the same concepts the ASL pilot already declares (and which
E11's `AgenticArchitecture` CRD already persists):

| Concept | agentic-lab | E11 contract (DriftWatch) | E13 uses it for |
|---|---|---|---|
| delegation edges | `AgentCard.can_delegate_to` (`models/agents.py:122`) | `canDelegateTo` | novel-edge / bypass check |
| hierarchy | `TacticalAgent.reports_to`, `ExecutionAgent.assigned_to` (`agents.py:211/231`) | `reportsTo`, `tier` | up-edge / skip-level check |
| four-tier | strategic / tactical / execution (`agents.py:185-247`) | `tier` | tier-ordering |
| scope monotonicity | "delegated scope ⊆ delegator scope" (`docs/concepts/authorization.md:62-80`) | `scope` | scope-escalation check |
| authority | Agent Card clearance + `can_access` (`security/agent_card.py:43`) | (E13 adds) | Aegis: clearance non-increasing |
| topology | pyramid (impl) / mesh (research) (`docs/topologies/`) | `topology` | which edges are legal |

So E13 adds **no new top-level org/delegation CRD** — the delegation graph itself is already declared
(E11's `AgenticArchitecture`). E13 reuses that graph and adds a **runtime scorer + credential
verifier** (as E12 added a scorer over the same CRD), plus **small runtime config** for the verifier
and the observation source (§5) — not a new declarative model, but not zero new surface either.

## 3. Observation — the core problem (and the "all agents in AutoGen/LangGraph" case)

A delegation `A → B` physically happens in one of four places. Only some are visible to an external
governor (this is the synthesis from the agentic-lab audit):

| Where the hand-off happens | Example | External governor sees it? |
|---|---|---|
| **In-process framework edge** | LangGraph `StateGraph` node→node, AutoGen `ConversableAgent` handoff | **NO** — a local function/edge, never on a wire |
| **Agent-as-tool over MCP** | Kagent `tools[].agent`, an agent exposed as an MCP tool | **YES (today)** — it's a `tools/call`, our proxy already sees it |
| **A2A cross-deployment** | gRPC + mTLS/DID between separate pods | **YES** — but cross-framework, that's **E14** |
| **Event bus** | CloudEvents on Kafka/NATS | **YES** — a subscriber sees it |

**The "everything is managed by AutoGen/LangGraph" scenario** (your question) lands squarely in row 1:
the orchestration is *inside* the framework runtime, so delegation is an in-process graph edge —
**no proxy, gateway, or admission controller can observe it.** Declaring `canDelegateTo` in ASL does
not, by itself, make the runtime hand-off visible. So E13 needs an explicit **observation strategy**,
not just a scorer.

### 3a. The abstraction: a normalized `DelegationEvent`

E13 introduces one small contract (mirrors the `RuntimeAdapter`/`ToolCall` pattern of E2/E3):

```
DelegationEvent { src_agent, dst_agent, task_scope, credential?, framework }
```

The **scorer is observation-agnostic** — it takes a `DelegationEvent` and checks it against the E11
contract + Aegis. What differs per deployment is **how the event is produced** — a pluggable
*delegation source*:

| Source | Captures | Effort | When |
|---|---|---|---|
| **(a) MCP hop** (existing proxy) | agent-as-tool delegations that flow as `tools/call` | low — reuse E10/E11 hop | **MVP** |
| **(b) Framework event-adapter** | in-process LangGraph/AutoGen hand-offs, via the framework's own callback/telemetry | medium-high, framework-specific | the "framework-managed" case |
| **(c) A2A proxy** | cross-deployment hand-offs | — | **E14** |

### 3b. Two honest answers for the framework-managed case

When agents are run by AutoGen/LangGraph, in-process delegation is invisible *unless* one of these is true:

1. **Instrument the framework (observe).** Attach to the framework's own hooks/telemetry —
   LangGraph emits node-transition callbacks / OTel spans; AutoGen has reply/handoff hooks. A
   thin, **framework-specific event-adapter** turns each hand-off into a `DelegationEvent` and feeds
   the same scorer. The framework keeps orchestrating; AgentGate observes + (optionally) vetoes.
   This is the only way to govern *truly in-process* delegation, and it is inherently per-framework
   (LangGraph adapter, AutoGen adapter, …) — matching agentic-lab's "framework-agnostic via
   generated adapters" vision (`docs/vision/framework-agnostic.md`), where generated code calls a
   governance seat at delegation points.
2. **Mediate the delegation (move the hop).** Declare cross-agent hand-offs as **agent-as-tool
   (MCP)** or **A2A** rather than in-process edges — then they pass through a governable hop (a),(c)
   and need no framework instrumentation. This is a *configuration* choice (how ASL/codegen wires
   delegation), trading a little in-process performance for an external, framework-neutral control
   point. agentic-lab's `enable_interceptors` / A2A-at-trust-boundaries posture points this way.

**E13's position (intra-framework, single-pod):** the **MVP is creation-driven** (§B — codegen bakes
the structural rules into the app, so the MVP needs no runtime observation at all). The
**observability-driven** mode is the follow-on for *runtime* drift inside the pod, and there the
right source is a **framework callback/hook** (LangGraph/AutoGen instrumentation), not an external
proxy — because in-pod hand-offs never reach a wire. Define the `DelegationEvent` seam so that hook
feeds the same scorer. (The **MCP agent-as-tool / A2A** sources in the table above are for
*cross-pod* hand-offs — that's **E14**, not this intra-framework slice.) Pure in-process delegation
that is *neither* baked-in *nor* instrumented is, by construction, ungovernable — we state that limit
plainly rather than pretend a proxy can see a `StateGraph` edge.

## 4. Scoring (observation-agnostic)

Given a `DelegationEvent`, the E11 `DeclaredContract`, and the **active delegation path** for the task:

1. **edge legality** — `contract.delegation_allowed(src, dst)` (already on the contract from E11) →
   novel-edge / bypass / up-edge via the declared graph + tiers.
2. **acyclicity (DAG)** — `dst` must not already be on the active delegation path → a hand-off that
   would close a cycle (`A → B → … → A`) is gated (runaway-delegation guard). The declared graph is
   itself validated acyclic at build time (§1).
3. **scope monotonicity** — `dst.task_scope ⊆ src.scope` (reuse the E11 `_scope_ok` helper).
4. **Aegis** — verify the hand-off credential (pluggable verifier; local-signer default, Keycloak
   optional) and clearance non-increasing (`src.clearance ≥ dst.clearance`).

A failure is a deterministic **declared** violation → `BLOCK`, emitted on the same `gen_ai.agent.*`
span with `gate.declared=true`, `anomaly.kind="delegation_violation"` (distinct from
`declared_violation`/`declared_sequence`). No contract / no delegation source → engine is exactly
E1–E12 (additive, standalone-safe).

## 4b. Telemetry emission (`gen_ai.agent.*`, CRD-configurable)

Every agent run emits one **`gen_ai.agent.*` span** (the same Observability-Summit schema as the
drift core — C1, no `drift.*`), so AgentGate shares one observability plane with DriftWatch:
`gen_ai.agent.id` / `task_type` / `model`, the bound `tools`, each tool call as a
`gen_ai.agent.tool.call` event (with `allowed`), and — on a delegation gate — `gate.declared=true` +
`anomaly.kind=delegation_violation`. The instrumentation scope is the service name (`agentgate`).

**Endpoint-only wiring:** the OTLP endpoint is the single knob — `AGENTGATE_OTLP_ENDPOINT` (Helm
`otel.endpoint`, e.g. the OTel Collector at `:4317`). Unset → no telemetry (just the `/run` trace);
set → traces to Jaeger, metrics to Prometheus/Grafana.

**CRD-configurable attribute set** — `AgenticArchitecture.spec.observability.otel.attributes` selects
*which* `gen_ai.agent.*` attributes are emitted, so an operator can trade detail for cost/noise:

| `attributes` value | Effect |
|---|---|
| *absent* / `["*"]` | emit **all** (default, back-compat) |
| `["none"]` | emit **nothing** (no span) — telemetry off for this org even if an endpoint is set |
| `["gen_ai.agent.id", "gen_ai.agent.gate.declared", …]` | emit **only** these |

C1-safe: this only ever emits a **subset** of the fixed `gen_ai.agent.*` schema — it never renames or
invents names. The attribute list lives on the declared contract (reconciled from the CRD), so it is
org-scoped and GitOps-able alongside the rest of the declaration.

## 4c. LLM cross-check drift (prompt-aware) — roadmap

> **Who selects the tool is fixed: the agent.** DriftWatch never does tool *selection* — Kagent /
> Goose / an AgentGate-generated app reasons with its own LLM and **selects**; DriftWatch (a proxy)
> only sees the resulting `tools/call` and **governs** it. So there is no "selection on/off" knob —
> selection always lives in the agent. The only thing this section adds is an optional second opinion
> on DriftWatch's side: a **prediction**, not a selection.

**Why** (the limit of statistical baseline): a per-`task_type` baseline assumes consistent tool
behavior per task. But the **prompt** often decides the chain shape — same `task_type`, one prompt is
a single call, another a multi-step chain — so a task-keyed baseline blurs them (false negatives).
Statistical baseline fits *repeating, low-variance* tasks; it is the wrong tool for *prompt-driven,
variable* chains.

**Idea** — a second, independent opinion at the call: the agent (AgentGate/Kagent) **selects** the
tool with its strong LLM; DriftWatch, given the **same prompt/context**, has a *light* LLM
**predict** the expected tool (it predicts, it does not select); the **divergence** between selection
and prediction is the drift signal. Prompt-aware (predicts from the prompt, not a task label), so it
catches what the task-keyed baseline can't. This is the **online, prompt-aware** form of the FR-9
model-panel consensus (offline "what's expected for this task" → online "what's expected for this
prompt").

```
agent (strong LLM) ── SELECTS tool + passes prompt/context ──▶ DriftWatch proxy
                                                                 ├─ light LLM PREDICTS expected tool(s)
                                                                 ├─ cross-check: selected vs predicted
                                                                 └─ divergence → danger_detected
```

**Three-layer governance** — an *additional* layer, not a baseline replacement; each catches a
different class:

| Layer | Cost | Determinism | Catches |
|---|---|---|---|
| Declared (E11/E12) | ~0 | deterministic | known-bad rules (bound/deny) — prompt-independent |
| Statistical baseline | low | semi | drift in a repeating task (narrow `task_type`) |
| **LLM cross-check** (this) | high | stochastic | prompt-aware, nuanced divergence (variable/novel tasks) |

**Making it sound (trade-offs):**
- **Cost** → a *light* model; **shadow-first** (flag, not inline block).
- **False positives** → don't trust one prediction: **N-vote** (drift only on majority divergence),
  suppress on low confidence.
- **Stochastic** → emit as a **score/flag** (`gen_ai.evaluation … danger_detected`), **never a hard
  block** — hard blocks stay on the deterministic declared layer.
- **Latency** → async/shadow, off the inline gate path.

**Split of work:** the agent passes the **prompt/context** alongside the tool call (in addition to
`task_type`); DriftWatch adds a `detection: llm-cross-check` mode (light model, N-vote, shadow
default) that predicts + compares + emits. The only knob is **cross-check on/off** (a *prediction*
toggle), never "who selects". Hard enforcement stays declared; this is a learned, prompt-aware
*signal*.

**Transport (confirmed feasible).** FastMCP `Client.call_tool(..., meta=…)` carries an MCP request
`_meta` map — so the agent-side context passing is wired today: AgentGate attaches
`{agent, task_type, prompt}` to every `call_tool` via the per-run `McpSession`. It is harmless until
DriftWatch reads it (the current proxy ignores unknown `_meta`). **Status:** agent-side context
passing **done** (meta on every call).

**DriftWatch-side detection (design).** A small, pure core module
(`interceptor/cross_check.py`) does the prediction independently of the proxy plumbing, so it is unit
testable without a live MCP hop:

- `predict_expected_tool(prompt, candidates, *, model, endpoint) -> str|None` — one light-LLM call
  (reuse the `httpx` Ollama `/api/chat` pattern from `codegen/runtime.py`): "given this prompt, which
  of these tools would you call? answer with one tool name". Returns the predicted tool or None
  (unreachable/unparseable → no signal, never crash).
- `cross_check(prompt, observed_tool, candidates, *, model, endpoint, votes=1) -> CrossCheckResult` —
  runs `predict` `votes` times (vary the call by index), majority-vote the prediction; **divergence =
  observed_tool not in the predicted set**. Returns `{diverged, predicted, votes, confidence}`.

Seams to wire it in (from the arch map):
| Seam | File | Change |
|---|---|---|
| read `_meta` | `interceptor/mcp_proxy.py` (`on_call_tool`) | extract `context.message._meta`, pass through |
| carry prompt | `interceptor/mcp_mapping.py` `to_engine_call(…, meta=None)` | put `meta` on the engine call |
| invoke detector | `interceptor/engine.py` `handle()` (post-`score_chain`) | if enabled + prompt present → `cross_check()` |
| emit | `otel/emit.py` + `otel/attributes.py` | `gen_ai.evaluation.name = danger_detected` (already defined), `anomaly.kind = llm_cross_check_mismatch` |
| config | `interceptor/main.py` | `DRIFTWATCH_CROSS_CHECK_ENABLED` (default false), `_MODEL`, `_ENDPOINT`, `_VOTES` |

Soundness (per §4c): **shadow-first** (emit only, never block — hard blocks stay declared), light
model, N-vote, graceful (no prompt / LLM down → no signal). **Status: done & live-confirmed.** Core
module + proxy/engine wiring + tests (agent-side `_meta` carries `{agent, task_type, prompt, tools}`
as an `_meta` arg key — FastMCP `call_tool(meta=)` is client-local; proxy lifts it out; engine runs
the shadow detector BEFORE the baseline gate so it fires even at cold-start; `emit_cross_check` →
`danger_detected`). End-to-end divergence confirmed in-cluster (driftwatch-mcp pod, real scenario: 12
candidates, gemma3:4b-cloud via host.k3d.internal): `diverged=True,
predicted=('k8s2_namespaces_list',), observed='k8s_namespaces_list'`. Live finding: bump predict
timeout to 90s (cloud light-model cold-start under concurrent load). The `/run` auto-flow log
visibility is intermittent only due to k3d proxy aggregate `list_tools` flakiness — unrelated to the
detector.

**Scope — cross-check needs the prompt, so it is AgentGate-scoped.** Cross-check requires the
prompt/context to reach DriftWatch (over MCP `_meta`). Only a client we control sends it:

| Agent | Sends prompt? | Governance available |
|---|---|---|
| **AgentGate-generated app** | ✅ yes — we own the client; prompt rides MCP `_meta` | declared + baseline + **cross-check** |
| **Kagent / Goose (raw)** | ❌ no — standard MCP client; prompt stays inside the agent | declared + baseline (cross-check **out of scope**) |

So cross-check is the **extra value AgentGate adds over a raw agent**: because AgentGate holds the
prompt, it can supply the second opinion. Raw Kagent/Goose get declared + baseline only; **if** they
later populate MCP `_meta` with the prompt, cross-check opens up to them too (optional, no DriftWatch
change). Either way, declared + baseline work for everyone — cross-check is additive on top.

### 4d. Hardening (consultant review)

Adversarial review düzeltmeleri (kabul edilenler):
- **Reconcile-time validation (#2):** declared graph artık yalnız codegen'de değil, **pod yükleme
  anında** (`agentgate/server.py` `_load_contract`) DAG + scope-monotonic doğrulanır — cyclic /
  scope-escalating org pod'u Ready yapmaz (`validate_for_generation` raise). (BLOCKER iddiası —
  suite timeout — geçersizdi: tam suite ~10s temiz, ASGITransport.)
- **`_meta` güvenliği (#3):** prompt `_meta` **yalnız governed (proxy, `namespace:false`) url'lere**
  gönderilir — direct MCP server'a prompt sızmaz; gerçek bir tool argümanı `_meta` ise **fail-fast**
  (sessizce ezme yok). `_GOVERNED_URLS` register'da `namespace=False` ile işaretlenir.
- **Cross-check latency (#4):** detector yalnız **FORWARD** yolunda çalışır (deny/cold-start kararını
  bekletmez) ve **toplam deadline** (`cc_timeout`, `DRIFTWATCH_CROSS_CHECK_TIMEOUT`, default 90s) tüm
  vote'ları kapsar — `votes × per-call` patlaması yok.
- **C1 şema:** tüm emit adları `otel/attributes.py` sabitlerinden; `emit_cross_check` artık
  `gen_ai.evaluation.result` event'ini (sabit) kullanır; AgentGate agent-run alanları
  (`gen_ai.request.model` upstream verbatim + `gen_ai.agent.tool.*` additive) sabit + dokümante.

Production-hardening turu (consultant 2. tur sonrası — **done**):
- **Explicit `governed` (#4):** `mcpServers[].governed` ayrı alan; prompt `_meta` artık `namespace`
  varsayımına değil **bu açık bayrağa** bağlı (default `not namespace`, geri uyum). Kırılgan eşleme
  kapandı (`register_mcp_tools(governed=)`, CRD alanı).
- **Register strict/readiness:** `register_mcp_tools(strict=)` + `AGENTGATE_MCP_STRICT` — unreachable
  backend artık `[]` ile sessizce değil, **raise** ile başarısız olur → pod Ready olmaz (prod);
  default off (dev/standalone degrade).
- **`check_delegation(strict=)`:** external-event scorer için **unknown src/dst = violation**
  (zero-trust); in-pod non-strict yol standalone-safe `None` kalır.
- **Dynamic-log quarantine (#5):** `make_router(allowed=)` declared olmayan `next`'i `__end__`'e
  yönlendirir — `log` modunda bile undeclared hand-off graph'a giremez; graph mapping güvenli.

Whole-backend **allowlist** (least-privilege) — **done**: `agent.mcpServers` artık string (tüm
tool'lar, default — geri uyum) **veya** `{name, allow, deny}` (glob'lar namespaced tool adları
üzerinde; allow önce, sonra deny). `mcp_backends` → `(name, allow, deny)` üçlüsü; `backend_tools_filtered`
glob filtreler; CRD `items: oneOf[string, object]`. Opt-in: `mcpServers: [k8s]` değişmez (tüm tool'lar
via governed proxy), isteyen `allow`/`deny` ile daraltır. Creation-driven korunur (filtrelenen tool
LLM'e hiç sunulmaz).

Kalan (tasarım kararı): `_meta` collision şu an **refuse + net mesaj** (overwrite engelli); hard-fail
yerine refuse (LLM halüsinasyonu run'ı düşürmesin).

## 4e. Govern: `proxyType` — single-source AgentGate ↔ DriftWatch interop

**Problem (chicken-and-egg).** Without this, the same `AgenticArchitecture` is declared twice: once
to AgentGate (to generate) and again to DriftWatch (to govern). Two sources, drift risk.

**Design.** One field on the org makes AgentGate the **single source**; DriftWatch is fed by **push**,
never re-declared:

```yaml
spec:
  govern:
    proxyType: driftwatch        # none (default) | driftwatch
    endpoint:  http://driftwatch-mcp.driftwatch.svc:8000/mcp   # governed tool path (MCP proxy)
    register:  http://driftwatch-interceptor.driftwatch.svc:8080/contracts  # one-time contract push
  agents: [...]
```

- **`proxyType: none`** (default / empty): AgentGate runs standalone — tools go direct, no governance.
  Nothing is sent to DriftWatch. (Back-compat: existing orgs are `none`.)
- **`proxyType: driftwatch`**: AgentGate, at server startup, does two things:
  1. **Push the contract once** — `build_contract(org).to_dict()` → `POST {register}` (idempotent;
     DriftWatch stores it as the declared contract, `source=agentgate`). The operator-`kubectl apply`
     of an `AgenticArchitecture` CR becomes unnecessary — AgentGate registers it.
  2. **Route the tool path** — agents' MCP tool calls go to `endpoint` (the DriftWatch proxy), so
     every call is governed (declared + baseline + cross-check). Equivalent to setting
     `spec.mcpServers` at the proxy, but implied by `proxyType`.

**Why it's clean.** Single declaration (AgentGate); push not pull (AgentGate emits, DriftWatch
receives); opt-in (`proxyType` is the one knob — `none`=standalone, `driftwatch`=governed). No code
dependency — `none` never touches DriftWatch; `driftwatch` is HTTP push + a proxy URL (protocol only).

**Split of work.** AgentGate: `govern.proxyType` field + startup push + tool-path routing.
DriftWatch: a contract **register endpoint** (`POST /contracts {contract}` → declared store, tagged
`source=agentgate`). Roadmap order: (a) design + CRD [this], (b) AgentGate push + routing, (c)
DriftWatch register endpoint.

## 5. CRD / config

No new top-level CRD — the delegation graph is already in `AgenticArchitecture` (E11). E13 adds:
- an **Aegis** block (verifier kind + issuer) — likely on the policy (`AgentDriftPolicy`) or the
  `AgenticArchitecture`; decide in review;
- a **delegation source** selector (mcp-hop | framework-adapter:<name>) so a deployment declares how
  hand-offs are observed;
- an **`observability.otel.attributes`** list (§4b) — `none` / `*` / explicit attribute allow-list,
  reconciled onto the declared contract;
- an **`mcpServers`** list (§ External tools) — `[{name, url}]`; `url` may point at an MCP server
  directly or at the DriftWatch proxy (the governance choice), reconciled onto the contract;
- a **`spec.llm`** (global default) + per-agent **`agent.llm`** override (§ Configurable LLM), and
  **`agent.instructionsFrom`** (configMapKeyRef | path) for config-sourced prompts.

## 6. Acceptance

### 6a. Creation-driven (single-pod MVP — the primary slice)
- [ ] **Declare fields** — `instructions` / `model` / `role` on the agent record (contract +
      `AgenticArchitecture` CRD + agentic-lab ASL) install + round-trip — unit-tested.
- [ ] **One generator (LangGraph)** — `AgenticArchitecture` → runnable app: `delegations` → edges,
      `instructions` → prompt, `tools` → bound tools, `model` → LLM, coordinator → entry point.
- [ ] **Creation-driven guarantee** — a `rules` deny edge is **not generated**; the declared graph is
      validated **acyclic (DAG)** and **scope-monotonic** at generate time (cyclic / escalating graph
      fails to generate) — unit-tested.
- [ ] **Execute** — the generated app runs in one pod; a goal posted to the coordinator flows down
      the declared graph and returns a result — smoke/e2e.

### 6d. External tools via MCP servers (dynamic binding)
- [x] `spec.mcpServers` reconciles onto the contract; at startup AgentGate lists each server's tools
      and registers a namespaced (`<server>_<tool>`) proxy callable — unit-tested (mocked client).
- [x] **Creation-driven binding holds** for MCP tools: an agent is offered only its declared MCP
      tools; an undeclared one is refused — unit-tested.
- [x] **Proxy vs direct** — pointing `url` at the DriftWatch proxy routes calls through chain-aware
      governance; pointing it at the server is direct. A connection failure degrades gracefully
      (tools absent, standalone-safe). *(live: ops → driftwatch-mcp proxy → real k8s namespaces)*
- [x] **Backend binding** — `agent.mcpServers: [backend]` gives the agent *all* of that backend's
      tools (no per-tool list); coexists with explicit `tools` — unit-tested. *(live: ops bound to k8s)*
- [x] **Namespace passthrough** — `mcpServers[].namespace: false` keeps the proxy's tool names
      verbatim (no `k8s_k8s_` double-prefix) — unit-tested. *(live: single-namespace tool names)*
- [x] **Startup robustness** — `register_mcp_tools` imports best-effort with a timeout; an
      unreachable/slow server registers nothing and the pod still starts — unit-tested.
- [x] **Chain grouping** — an agent-run's MCP calls reach the proxy as one chain (one MCP session per
      run, reused), so DriftWatch correlates them by session id — unit-tested. *(live: 4 tools, one run)*

### 6e. Configurable LLM + instruction sourcing
- [x] **Global + per-agent LLM** — `spec.llm` default; `agent.llm` overrides per field; resolution
      `agent.llm ?? agent.model ?? spec.llm ?? env` — unit-tested. Back-compat: existing `agent.model`
      + env path unchanged. *(live: global 397b-cloud + coder override, no LLM env)*
- [x] **Instruction sourcing** — `instructions` (inline) vs `instructionsFrom` (configMapKeyRef |
      path); effective = inline ?? loaded ?? default — unit-tested.

### 6b. Observability-driven (runtime drift — follow-on)
- [ ] `DelegationEvent` + an observation-agnostic scorer (`check_delegation`) over the E11 contract:
      novel-edge, bypass, up-edge, scope-escalation — unit-tested.
- [ ] **Runtime DAG cycle** — a hand-off re-entering an agent already on the active delegation path is
      gated — unit-tested.
- [ ] Aegis verifier interface + local-signer impl; forged/expired credential blocked; clearance
      non-increasing — unit-tested.
- [ ] **Observation source** — for the single-pod case, a framework callback/hook produces
      `DelegationEvent`s; an undeclared hand-off is flagged/blocked — in-cluster e2e. (Cross-pod /
      MCP-hop source is E14-adjacent.)
- [ ] OTel: `gate.declared=true`, `anomaly.kind="delegation_violation"`.
- [ ] No contract / no source → engine == E1–E12 (standalone unchanged).

### 6c. Telemetry emission (§4b)
- [ ] Each agent run emits a `gen_ai.agent.*` span (id/task/model/tools + tool.call events) to the
      OTel endpoint; scope = `agentgate`; verified live (Jaeger).
- [ ] **CRD-configurable attributes** — `observability.otel.attributes`: `["none"]` → no span;
      `["*"]`/absent → all; explicit list → only those (identity-safe) — unit-tested.

## 7. Out of scope
- **A2A / cross-framework** hand-offs and the over-graph → **E14**.
- **Full LangGraph/AutoGen event-adapters** beyond the seam + one reference adapter → follow-up.
- **Required/expected** delegation patterns ("behavioral envelopes", MABaC behavioral metadata) →
  roadmap; E13 is deny/contract enforcement, not positive behavior modelling.

## 8. Open decisions (settle in review)
1. **Codegen home** — does the per-framework generator live in **agentic-lab** (`generators/`, where
   LangGraph/native already exist) or in driftwatch/agentgate (single product)? This decides which
   repo the generator code lands in. *(Recommended: agentic-lab owns Generate; driftwatch owns Govern;
   shared YAML is the contract.)*
2. **First framework** — LangGraph (most common, agentic-lab base exists) vs AutoGen.
3. **Aegis verifier** — local-signer only in v1alpha1, Keycloak as the pluggable second?
4. **Where Aegis/observation-source config lives** — `AgentDriftPolicy` vs `AgenticArchitecture`.
5. **In-pod veto vs observe-only** — for the observability-driven hook, can AgentGate *block* a
   hand-off synchronously or only *flag* it (async telemetry)? Bounds what "enforce" means for the
   runtime mode (creation-driven already blocks structurally, at generate time).

## 9. Implementation phases (how we build it)

| # | Phase | Repo | Output |
|---|---|---|---|
| **0** | Design (this doc) — lock the frame | — | this file |
| **1** | **Declare fields** — add `instructions`/`model`/`role` to the agent record | driftwatch (`contract.py` + CRD) **+** agentic-lab (ASL) | 10 agents, all fields, in one YAML |
| **2** | **Generate — one framework (LangGraph)** — YAML → runnable app | agentic-lab (`generators/`) | `generate org.yaml --target langgraph` → single-pod app |
| **3** | **Creation-driven guarantee** — deny edge not generated; build-time DAG + scope validation | agentic-lab | test: forbidden hand-off impossible; cyclic/escalating graph rejected |
| **4** | **Execute** — package as one pod; coordinator entry (`POST /run`) | driftwatch (deploy) / agentic-lab (runtime) | goal → coordinator → graph → result, in-cluster |
| **5** | **More frameworks** — AutoGen, CrewAI generators | agentic-lab | same YAML → 3 frameworks |
| **6** | **Observability-driven** — `DelegationEvent` + scorer + framework hook + Aegis | driftwatch | runtime drift caught inside the pod (adds to, not replaces, creation-driven) |

**MVP = phases 1–4** (declare → generate LangGraph → creation-driven guarantee → execute in one pod):
the full "one YAML → a running, governed, single-framework multi-agent app". Phases 5–6 broaden
frameworks and add runtime drift detection on top.
