# E12 — Declared chain-rules (deny sequences) (design + status)

**Status: implemented + validated in-cluster** (k3d). Commit `e37c181`; repeatable e2e in
`examples/k3d-cluster-demo/declared-layer-e2e.sh` (`make declared-e2e`) on the `demo-org` contract:
an **unbound** tool (`k8s_pods_delete`) is declared-blocked (E11) and the **deny ordering**
`k8s_namespaces_list → k8s_pods_list` is declared-blocked (E12), while a single bound call passes.
E12 is the **sequence-level** half of the declared layer.
E11 declares *what an agent may touch* (tool bindings, scope) and checks each call in isolation;
E12 declares *what orderings are forbidden* — hand-written, deterministic **deny sequences** that
catch known-bad chains a per-call check can't see. "Declared rules catch known-bad (deterministic);
learned baselines catch unknown-bad (statistical). Production needs both."

## Where it lives — on the same `AgenticArchitecture` contract

E12 extends the E11 contract, not a new CRD. `AgenticArchitecture.spec` gains a `rules` list of
deny sequences; the operator folds them into the same `DeclaredContract` it already persists, and
the engine checks them on the same hot path as the E11 single-call check (before the statistical
baseline). One contract, one reconcile, one declared check with two facets (call + sequence).

```yaml
apiVersion: driftwatch.graphsentinel.org/v1alpha1
kind: AgenticArchitecture
metadata: { name: demo-org, namespace: agents }
spec:
  tools:   [ ... ]                 # E11 catalogue (+risk)
  agents:  [ ... ]                 # E11 bindings/scope/delegation
  rules:                           # E12 — declared chain-rules (deny sequences)
    - deny: [k8s_pods_list, k8s_pods_delete]   # forbidden ordering (known-bad recon→destroy)
      reason: "list-then-delete is destructive reconnaissance"
      agent: pod-reaper            # optional: scope the rule to one agent (default: any)
    - deny: [k8s_secrets_list, k8s_pods_exec]
      reason: "secret-read then exec is exfiltration-shaped"
```

> The YAML above is **illustrative** (an intuitive list-then-delete example). The **validated,
> repeatable** demo lives in `examples/k3d-cluster-demo/manifests/agenticarchitecture-demo.yaml`
> (`make declared-e2e`), where the deny ordering is `k8s_namespaces_list → k8s_pods_list` — chosen
> so E11 (unbound `k8s_pods_delete`) and E12 (the deny ordering) are demonstrated separately.

## Matching semantics (binding decisions)

- **D1 — contiguous suffix match.** A rule `deny: [A, B]` fires when the chain's **most recent
  calls end with `A, B` in order** (the call being scored is the last element). Contiguous (not
  subsequence) keeps it deterministic and cheap, and matches the intent "A *then immediately* B".
  Longer chains still match on their tail; earlier unrelated calls don't matter.
- **D2 — fires before the statistical score**, like E11: a declared sequence violation is a
  deterministic known-bad → `BLOCK` regardless of baseline readiness. No contract/rules → engine
  is exactly E1–E10 (standalone).
- **D3 — namespaced tool names.** Rules use the same tool ids the engine sees (cross-server:
  `<server>_<tool>`), consistent with the E11 catalogue — one naming domain.
- **D4 — optional `agent` scope.** A rule with `agent: X` only applies when the chain's agent is X
  (matches the E11 contract key); without it, the rule applies to any agent. So org-wide and
  agent-specific rules coexist.
- **D5 — precedence: E11 then E12.** The single-call check (unbound/out-of-scope, E11) runs first;
  if it passes, the sequence check (E12) runs; if both pass, the statistical baseline runs. First
  deterministic violation wins, so the most specific known-bad reason is reported.
- **D6 — risk is advisory, not required.** Rules are explicit tool sequences (legible, auditable).
  The catalogue `risk` map (E11) is available for a future "deny any sequence ending in risk≥4
  after a read" heuristic, but v1alpha1 ships **explicit sequences only** — no implicit risk rules,
  to keep the declared layer surprise-free.

## Engine

`DeclaredContract` gains `deny_sequences: list[DenyRule]` and a
`check_sequence(agent_id, recent_tools) -> reason | None`. In `Interceptor.handle`, after the E11
single-call check and before scoring:

```
reason = contract.check(agent_id, tool, scope)          # E11 single-call
if reason: -> declared BLOCK
reason = contract.check_sequence(agent_id, chain.tools)  # E12 sequence (tail match)
if reason: -> declared BLOCK (anomaly_kind="declared_sequence")
... statistical baseline ...
```

`emit_declared` is reused; the sequence violation rides the same `gen_ai.agent.*` span with
`gate.declared=true` and `computed.anomaly.kind="declared_sequence"` (vs `declared_violation` for
the single-call case), so the two declared facets are distinguishable in tooling.

## Acceptance (what E12 adds) — all met
- [x] `AgenticArchitecture.spec.rules` (deny sequences, optional per-agent) installs + validates.
- [x] Operator folds rules into the persisted `DeclaredContract` (same reconcile + contractHash).
- [x] A chain whose tail matches a deny sequence is **declared-blocked** before scoring; a chain
      that doesn't is unaffected; the single non-final call of a deny pair is not blocked.
- [x] Per-agent rule applies only to its agent; org-wide rule applies to all.
- [x] No rules → engine == E11/E1–E10.
- [x] OTel: `gate.declared=true`, `anomaly.kind="declared_sequence"`.
- [x] Unit tests: tail match / no-match / non-final-call / per-agent scope / standalone; **+
      in-cluster e2e** (k3d: apply rule → forbidden ordering declared-blocked, single call passes).

## Out of scope
- Implicit risk-based sequence rules (D6) — heuristic, later.
- Cross-agent sequences (a sequence spanning a delegation hand-off) — that is E13's delegation
  graph, not a single agent's chain.
- Allow-lists / required sequences — E12 is deny-only (known-bad); "expected behavior" envelopes
  are MABaC/behavioral-metadata roadmap.
