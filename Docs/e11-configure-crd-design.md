# E11 — Configure / declare layer: `AgenticArchitecture` CRD (design + status)

**Status: implemented + validated in-cluster** (k3d). The optional `agentlab → driftwatch-crd`
generator (ASL front-end → CR) remains roadmap. E11 is the **declare** half of the AgentGate
lifecycle (*Declare → Observe → Enforce*). It makes the **agent organization itself** a
Kubernetes-native, version-controlled contract — the same way E1–E10 already make *drift policy* a
CRD. Commits: `5b53781` (CRD + declared-check), `dbb2521` (Helm render), `d4fa294` (persist chain),
`f55303f` (OTel + duplicate guard), `238fb90` (in-cluster e2e gaps: agent_id, RBAC, declared msg).

## Where this sits

- **AgentGate** (the platform) = Declare (E11) + Observe/Enforce (DriftWatch core E1–E10) +
  declared-rules (E12) + delegation (E13). **DriftWatch is AgentGate's drift-detection subset.**
- **E11 is DriftWatch/AgentGate's own CRD** — `AgenticArchitecture`, in the existing
  `driftwatch.graphsentinel.org/v1alpha1` group (consistent with `AgentDriftPolicy`). The operator
  reconciles it into the **declared contract** that the engine scores *actual* behavior against.
- **Standalone:** the platform needs no external tool — you `kubectl apply` an `AgenticArchitecture`
  CR and the operator reconciles it. **`agentic-lab` (ASL) is an *optional* declare-time front-end**
  that *authors/generates* this CR (and app scaffolds); it is not a runtime dependency.

So there are two ways to get the contract in, same schema:
1. hand-write / GitOps an `AgenticArchitecture` CR → `kubectl apply` (DriftWatch standalone), or
2. `agentlab generate --target driftwatch-crd org.asl.yaml` → the same CR (agentic-lab front-end).

## The CRD — `AgenticArchitecture`

`driftwatch.graphsentinel.org/v1alpha1`, **Namespaced**, `shortNames: [aa]`. The spec mirrors ASL's
**four-tier hierarchy** (tools catalogue + strategic + tactical + execution) but keeps only what the
**governance engine** needs to score declared-vs-actual — not codegen/runtime fields (those stay in
agentic-lab). Minimal first cut:

```yaml
apiVersion: driftwatch.graphsentinel.org/v1alpha1
kind: AgenticArchitecture
metadata: { name: acme-org, namespace: agents }
spec:
  # shared tools catalogue — declared once, bound per-agent
  tools:
    - name: k8s_pods_list      # tool id as the engine sees it (gen_ai.tool.name)
      category: k8s
      risk: 1                  # 0 safe .. 4 destructive (feeds the E12/D3 guard too)
    - name: k8s_pods_delete
      category: k8s
      risk: 4
  # agents across the hierarchy; each declares its allowed surface (the contract)
  agents:
    - name: ops-orchestrator
      tier: strategic          # strategic | tactical | execution
      clearance: confidential  # low_public | internal_restricted | confidential | top_secret
      reasoning: llm           # llm | deterministic
      tools: [k8s_pods_list]               # bound tools (least-privilege)
      scope: ["ns:agents", "ns:demo"]      # resource scope boundary
      canDelegateTo: [pod-reaper]          # allowed delegation edges (E13 will score these)
    - name: pod-reaper
      tier: execution
      reportsTo: ops-orchestrator          # hierarchy edge
      clearance: internal_restricted
      reasoning: deterministic
      tools: [k8s_pods_list, k8s_pods_delete]
      scope: ["ns:demo"]
  topology: pyramid            # pyramid | mesh
status:
  reconciled: true
  agents: 2
  contractHash: "…"           # so AgentDriftPolicy can reference a specific contract version
```

Field choices (honest scope):
- **`tools[]` + per-agent `tools` (bindings) + `scope`** → the *allowed surface* the engine treats
  as the declared contract: a call to a tool not bound to the agent, or outside `scope`, is a
  **declared violation** (deterministic), distinct from a *statistical* drift.
- **`risk`** on the catalogue is reused by the E12 declared-rules / D3 destructive-retry guard — one
  source of truth for tool risk (today the MCP hop has no catalog → risk 0; this fills it).
- **`canDelegateTo` / `reportsTo`** → the delegation graph **E13 (MABaC)** will score; declared here
  in E11, enforced in E13.
- **Out of scope for E11 (kept in agentic-lab / later):** codegen targets, Dockerfile/K8s scaffold,
  edge-sync, protocol wiring, PII/filter runtime, **behavioral metadata (expected sequences /
  confidence floors)** — that last one is E13/MABaC roadmap.

## Operator reconcile

Same pattern as `AgentDriftPolicy` (kopf handler in `operator/`):
1. On create/update of an `AgenticArchitecture`, build a **declared contract** object: per-agent
   `{allowed_tools, scope, delegation_edges, risk_map}`.
2. Persist it where the engine reads policy (the operator-written store the MCP proxy / interceptor
   already mount read-only) — keyed so an `AgentDriftPolicy` can opt to enforce against it.
3. Set `status.reconciled`, `status.contractHash`.
4. The engine, when scoring a call, can now also check the **declared** surface (tool bound? in
   scope?) — a *known-bad* deterministic check **alongside** the statistical baseline. This is the
   hook E12 (declared chain-rules) builds on.

No change to the detection core's statistical path; E11 adds a **declared** signal next to it
("declared catches known-bad; learned catches unknown-bad — production needs both").

## DriftWatch-standalone guarantee

- `AgenticArchitecture` is optional: an `AgentDriftPolicy` with no referenced contract behaves
  exactly as today (pure statistical drift — the KubeCon/DriftWatch story).
- With a contract referenced, the same engine adds the declared check (the AgentCon/AgentGate story).
- **Framework-agnostic:** the CRD is the *Kubernetes* way to supply the contract; off-Kubernetes the
  same declared-contract object can be loaded from the ASL YAML directly (no CRD) — one schema, two
  delivery paths.

## Acceptance (what E11 adds) — all met
- [x] `AgenticArchitecture` CRD installs (Helm renders it); a CR validates (four-tier agents, tool
      bindings, scope, delegation edges, risk).
- [x] Operator reconciles a CR into a declared-contract object + `status.contractHash`, **persisted**
      to the data plane (`<DATA_DIR>/contracts/<name>.json`).
- [x] An `AgentDriftPolicy` can reference a contract (`contractRef`); a call to an **unbound tool**
      or **out-of-scope** is flagged as a *declared* violation (deterministic), separate from
      statistical drift, and emitted as `gen_ai.agent.*` with `gate.declared=true`.
- [x] No contract referenced → engine behaves exactly as E1–E10 (DriftWatch standalone unchanged).
- [x] Unit tests: CRD schema / build / reconcile / declared-vs-within / standalone / persist
      round-trip / duplicate-name guard. **In-cluster e2e (k3d):** apply CR → operator reconcile +
      PVC persist → proxy load (`DRIFTWATCH_CONTRACT_REF` + `DRIFTWATCH_AGENT_ID`) → bound forwards,
      unbound declared-blocked.

## Naming / rename note
Today everything stays in `driftwatch.graphsentinel.org/v1alpha1` for consistency with the shipping
`AgentDriftPolicy`. The platform-wide **DriftWatch → AgentGate** rename (group → `agentgate.…`,
package → `agentgate` with `agentgate.drift` as the DriftWatch subset) is a **single later pass**,
cheap at v1alpha1 (no prod), done once both talks are accepted. Write modular now, rename once later.

## Out of scope
- E12 (declared *sequence* rules — `deny: sequence[...]`) and E13 (delegation-graph scoring / Aegis)
  consume this contract; they are separate epics on this branch.
- agentic-lab's `--target driftwatch-crd` generator (the optional front-end) — Zeyno's side.
