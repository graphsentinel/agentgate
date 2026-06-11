"""Declared contract — the configure/declare layer (E11).

An `AgenticArchitecture` declares the agent organization: a shared tool catalogue (with risk),
and per-agent tool bindings, scope, and delegation edges. The operator reconciles a CR into a
`DeclaredContract`; the engine then runs a **declared check** alongside the statistical baseline:
a call to a tool an agent is not bound to, or outside its declared scope, is a *declared violation*
(deterministic, known-bad) — distinct from a statistical drift (learned, unknown-bad).

Pure and transport-free: built from a plain spec dict, so the same builder serves both the
Kubernetes CRD path (operator) and an off-cluster ASL YAML (framework-agnostic). No dependency on
the detection core — declared and statistical signals stay independent.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class AgentContract:
    """One agent's declared surface: what it may call, where, and whom it may delegate to."""

    name: str
    allowed_tools: frozenset[str] = frozenset()   # bound tool names (least-privilege)
    scope: frozenset[str] = frozenset()           # allowed scope prefixes ("" = unconstrained)
    can_delegate_to: frozenset[str] = frozenset() # declared delegation edges (E13 scores these)
    reports_to: str | None = None                 # hierarchy edge (E13)
    tier: str = ""                                # strategic | tactical | execution
    # E13 generation fields — NOT used by the declared-check (governance stays opaque to the prompt);
    # carried through so codegen can emit a runnable app (Docs/e13-mabac-delegation-design.md §B).
    role: str = ""                                # one-line human summary
    model: str = ""                               # LLM id the generator wires into the agent
    instructions: str = ""                        # the agent's prompt body (opaque to governance)
    llm_provider: str = ""                        # per-agent LLM provider override (else global/env)
    llm_endpoint: str = ""                        # per-agent LLM endpoint override (else global/env)
    # whole-backend binding: (name, allow, deny) per backend. allow/deny are glob patterns over the
    # registered (namespaced) tool names (empty allow = all; deny removes). A bare backend name parses
    # to (name, (), ()) = all tools (back-compat / vision-preserving default).
    mcp_backends: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class DenyRule:
    """A declared chain-rule (E12): a forbidden contiguous tool ordering (known-bad)."""

    sequence: tuple[str, ...]          # the forbidden contiguous tail (e.g. ("pods_list","pods_delete"))
    reason: str = ""
    agent: str | None = None           # None = applies to any agent; else only this agent


def _scope_ok(call_scope: str, allowed: frozenset[str]) -> bool:
    """True if a call's scope is within the agent's declared scope.

    Empty `allowed` = unconstrained (agent declared no scope boundary). A scope matches when it
    equals a declared scope or is nested under it (`ns:demo` ⊆ `ns:demo/...`). Empty call scope is
    always allowed (the call targets nothing scoped).
    """
    if not allowed or not call_scope:
        return True
    return any(call_scope == s or call_scope.startswith(s + "/") for s in allowed)


def _parse_backend(b: object) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Parse one whole-backend binding to (name, allow, deny).

    A bare string is "all tools" (back-compat / default): `"k8s"` → `("k8s", (), ())`. An object
    narrows it: `{name, allow, deny}` → globs over the namespaced tool names (consultant allowlist).
    """
    if isinstance(b, str):
        return (b, (), ())
    if isinstance(b, dict):
        return (b.get("name", "") or "",
                tuple(b.get("allow", []) or []), tuple(b.get("deny", []) or []))
    return ("", (), ())


def _scope_subset(child: frozenset[str], parent: frozenset[str]) -> bool:
    """True if `child` scope ⊆ `parent` scope (a delegation must not widen scope, E13).

    Empty `parent` = unconstrained → any child is fine. Empty `child` under a constrained parent is a
    widening → not a subset. Else every child prefix must sit under some parent prefix.
    """
    if not parent:
        return True
    if not child:
        return False
    return all(any(c == p or c.startswith(p + "/") for p in parent) for c in child)


@dataclass(frozen=True)
class DeclaredContract:
    """The whole organization's declared contract, keyed by agent name."""

    agents: dict[str, AgentContract] = field(default_factory=dict)
    risk_map: dict[str, int] = field(default_factory=dict)   # tool name -> risk tier (0..4)
    topology: str = ""                                       # pyramid | mesh
    deny_sequences: tuple[DenyRule, ...] = ()                # E12 declared chain-rules
    # observability.otel.attributes — which gen_ai.agent.* attrs to emit per agent run:
    #   () / absent   -> emit ALL (back-compat)
    #   ("none",)     -> emit NOTHING (no span), even if an endpoint is set
    #   ("*",)        -> emit ALL
    #   ("a","b",...) -> emit ONLY these
    emit_attributes: tuple[str, ...] = ()
    # external MCP tool sources (E13 §External tools): (name, url, namespace, governed) tuples. `url`
    # may target an MCP server directly or the DriftWatch chain-aware proxy. namespace=True → tools
    # imported as <name>_<tool>; False → keep names verbatim (passthrough). governed=True → this url is
    # behind the DriftWatch proxy, so cross-check prompt `_meta` may be sent to it (explicit, decoupled
    # from namespace per consultant #4); default governed = (not namespace) for back-compat.
    mcp_servers: tuple[tuple[str, str, bool, bool], ...] = ()
    # global LLM default (E13 §Configurable LLM) — per-agent fields override these, env is the floor.
    llm_provider: str = ""
    llm_model: str = ""
    llm_endpoint: str = ""

    def effective_llm(self, agent_id: str) -> tuple[str, str, str]:
        """Resolve (provider, model, endpoint) for an agent: per-agent ?? global (env is the runtime
        floor, applied in the runtime). Most specific wins; empty falls through."""
        a = self.agents.get(agent_id)
        provider = (a.llm_provider if a else "") or self.llm_provider
        model = (a.model if a else "") or self.llm_model
        endpoint = (a.llm_endpoint if a else "") or self.llm_endpoint
        return provider, model, endpoint

    def check(self, agent_id: str, tool: str, scope: str = "") -> str | None:
        """Return a declared-violation reason, or None if the call is within the contract.

        An agent not present in the contract is **unconstrained for this single-call check**
        (returns None) — so a policy with no/partial contract degrades to pure statistical drift
        (DriftWatch-standalone). Note: this is only the per-call (bindings/scope) facet; **org-wide
        deny-sequences (E12, rules with no `agent`) still apply to any agent** via `check_sequence`.
        """
        ac = self.agents.get(agent_id)
        if ac is None:
            return None  # not declared → no per-call constraint (org-wide sequence rules still apply)
        if ac.allowed_tools and tool not in ac.allowed_tools:
            return f"tool {tool!r} is not bound to agent {agent_id!r} (declared violation)"
        if not _scope_ok(scope, ac.scope):
            return f"scope {scope!r} is outside the declared scope of agent {agent_id!r}"
        return None

    def check_sequence(self, agent_id: str, tools: list[str]) -> str | None:
        """Return a declared deny-sequence violation reason, or None (E12).

        A rule `deny: [A, B]` fires when the chain's most recent calls **end with** that exact
        contiguous ordering (the call just scored is the last element) — deterministic known-bad.
        A rule scoped to an agent only applies to that agent. No rules / no match → None.
        """
        for rule in self.deny_sequences:
            if rule.agent is not None and rule.agent != agent_id:
                continue
            n = len(rule.sequence)
            if n and len(tools) >= n and tuple(tools[-n:]) == rule.sequence:
                msg = f"declared deny-sequence {list(rule.sequence)}"
                return f"{msg}: {rule.reason}" if rule.reason else msg
        return None

    def delegation_allowed(self, src: str, dst: str) -> bool:
        """Whether `src` may delegate to `dst` per the declared graph (E13 will score this)."""
        ac = self.agents.get(src)
        return ac is not None and dst in ac.can_delegate_to

    def check_delegation(
        self, src: str, dst: str, active_path: tuple[str, ...] | list[str] = (),
        *, strict: bool = False,
    ) -> str | None:
        """E13 — score a *runtime* hand-off `src -> dst` against the declared graph. reason | None.

        For dynamic/conditional graphs where an orchestrator chooses the next agent at run time (so
        the hand-off is not fixed at generate time). Catches: a hand-off not in the declared graph
        (novel edge), one re-entering an agent already on the active delegation path (cycle), and one
        that widens scope (`dst.scope ⊄ src.scope`, scope escalation).

        `strict=False` (default): an unknown `src` is unconstrained (None) — standalone-safe, like
        `check`; the in-pod generator always passes its own declared name. `strict=True` (consultant:
        for an EXTERNAL DelegationEvent channel where the source can't be trusted): an unknown `src`
        or `dst` is itself a VIOLATION — a spoofed/unknown party cannot bypass governance (zero-trust).
        """
        ac = self.agents.get(src)
        if ac is None:
            return (f"delegation from unknown source {src!r}" if strict
                    else None)  # non-strict: src not declared → no constraint (degrades cleanly)
        if strict and dst not in self.agents:
            return f"delegation to unknown target {dst!r}"
        if dst not in ac.can_delegate_to:
            return f"delegation {src!r} -> {dst!r} is not in the declared graph (novel edge)"
        if dst in tuple(active_path):
            return f"delegation {src!r} -> {dst!r} re-enters an active agent (cycle)"
        d = self.agents.get(dst)
        if d is not None and not _scope_subset(d.scope, ac.scope):
            return (f"delegation {src!r} -> {dst!r} escalates scope "
                    f"({sorted(d.scope)} ⊄ {sorted(ac.scope)})")
        return None

    def to_dict(self) -> dict:
        """Serialize to a plain dict (for persistence; the inverse of `from_dict`)."""
        return {
            "agents": {
                n: {
                    "allowed_tools": sorted(a.allowed_tools),
                    "scope": sorted(a.scope),
                    "can_delegate_to": sorted(a.can_delegate_to),
                    "reports_to": a.reports_to,
                    "tier": a.tier,
                    # E13 generation fields (omitted when empty to keep E11/E12 contracts byte-stable)
                    **({"role": a.role} if a.role else {}),
                    **({"model": a.model} if a.model else {}),
                    **({"instructions": a.instructions} if a.instructions else {}),
                    **({"llm_provider": a.llm_provider} if a.llm_provider else {}),
                    **({"llm_endpoint": a.llm_endpoint} if a.llm_endpoint else {}),
                    **({"mcp_backends": [
                        {"name": n, **({"allow": sorted(al)} if al else {}),
                         **({"deny": sorted(dn)} if dn else {})}
                        for n, al, dn in a.mcp_backends]} if a.mcp_backends else {}),
                }
                for n, a in sorted(self.agents.items())
            },
            "risk_map": dict(sorted(self.risk_map.items())),
            "topology": self.topology,
            "deny_sequences": [
                {"deny": list(r.sequence), "reason": r.reason, "agent": r.agent}
                for r in self.deny_sequences
            ],
            # omit when empty so E11/E12 contracts serialize byte-identically (contractHash stable)
            **({"emit_attributes": list(self.emit_attributes)} if self.emit_attributes else {}),
            **({"mcp_servers": [{"name": n, "url": u, "namespace": ns, "governed": gv}
                                 for n, u, ns, gv in self.mcp_servers]}
               if self.mcp_servers else {}),
            **({"llm": {k: v for k, v in (("provider", self.llm_provider),
                                          ("model", self.llm_model),
                                          ("endpoint", self.llm_endpoint)) if v}}
               if (self.llm_provider or self.llm_model or self.llm_endpoint) else {}),
        }

    @classmethod
    def from_dict(cls, d: dict) -> DeclaredContract:
        """Rebuild from `to_dict` output (what the interceptor loads from the data plane)."""
        agents = {
            n: AgentContract(
                name=n,
                allowed_tools=frozenset(a.get("allowed_tools", []) or []),
                scope=frozenset(a.get("scope", []) or []),
                can_delegate_to=frozenset(a.get("can_delegate_to", []) or []),
                reports_to=a.get("reports_to"),
                tier=a.get("tier", "") or "",
                role=a.get("role", "") or "",
                model=a.get("model", "") or "",
                instructions=a.get("instructions", "") or "",
                llm_provider=a.get("llm_provider", "") or "",
                llm_endpoint=a.get("llm_endpoint", "") or "",
                mcp_backends=tuple(_parse_backend(b) for b in (a.get("mcp_backends", []) or [])),
            )
            for n, a in (d.get("agents", {}) or {}).items()
        }
        return cls(
            agents=agents,
            risk_map={k: int(v) for k, v in (d.get("risk_map", {}) or {}).items()},
            topology=d.get("topology", "") or "",
            deny_sequences=tuple(
                DenyRule(sequence=tuple(r.get("deny", []) or []),
                         reason=r.get("reason", "") or "", agent=r.get("agent"))
                for r in (d.get("deny_sequences", []) or []) if r.get("deny")
            ),
            emit_attributes=tuple(d.get("emit_attributes", []) or []),
            mcp_servers=tuple((s["name"], s["url"], s.get("namespace", True),
                               s.get("governed", not s.get("namespace", True)))
                              for s in (d.get("mcp_servers", []) or [])
                              if s.get("name") and s.get("url")),
            llm_provider=(d.get("llm", {}) or {}).get("provider", "") or "",
            llm_model=(d.get("llm", {}) or {}).get("model", "") or "",
            llm_endpoint=(d.get("llm", {}) or {}).get("endpoint", "") or "",
        )

    @property
    def hash(self) -> str:
        """Stable hash of the contract (for `status.contractHash` / policy references)."""
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode()
        ).hexdigest()[:16]


# --- persistence: operator writes, interceptor/proxy reads (same data-plane as the baseline) ---

def _contracts_dir(data_dir: str) -> Path:
    return Path(data_dir) / "contracts"


def save_contract(contract: DeclaredContract, data_dir: str, name: str) -> Path:
    """Persist a contract as JSON under `<data_dir>/contracts/<name>.json` (operator side, write).

    Mirrors the baseline-store layout (operator writes to the mounted writable volume,
    `DRIFTWATCH_DATA_DIR`); the interceptor/proxy mounts the same path read-only and loads it.
    """
    d = _contracts_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    path.write_text(json.dumps(contract.to_dict(), sort_keys=True))
    return path


def load_contract(data_dir: str, name: str) -> DeclaredContract | None:
    """Load a single contract by name from the data plane (interceptor side, read-only).

    Returns None if absent — so a policy referencing a not-yet-reconciled contract degrades to
    pure statistical drift (standalone-safe) rather than failing.
    """
    path = _contracts_dir(data_dir) / f"{name}.json"
    if not path.exists():
        return None
    return DeclaredContract.from_dict(json.loads(path.read_text()))


def delete_contract(data_dir: str, name: str) -> None:
    """Remove a persisted contract (operator side, on CR delete) — no error if already gone."""
    (_contracts_dir(data_dir) / f"{name}.json").unlink(missing_ok=True)


def resolve_instructions(spec: dict) -> dict:
    """Resolve each agent's `instructionsFrom` (configMapKeyRef | path) into `instructions`, in place.

    Called by the server/CLI *before* `build_contract`, so file/ConfigMap reads stay out of the pure
    builder. `path` is read directly; `configMapKeyRef.key` resolves to `<AGENTGATE_PROMPTS_DIR>/<key>`
    (the chart mounts the prompts ConfigMap there, default `/etc/agentgate/prompts`). Inline
    `instructions` always win; a missing/unreadable source is left as-is (degrades to default).
    """
    import os
    prompts_dir = os.environ.get("AGENTGATE_PROMPTS_DIR", "/etc/agentgate/prompts")
    for a in spec.get("agents", []) or []:
        if a.get("instructions"):
            continue
        ref = a.get("instructionsFrom") or {}
        path = ref.get("path") or (
            os.path.join(prompts_dir, ref["configMapKeyRef"]["key"])
            if (ref.get("configMapKeyRef") or {}).get("key") else None
        )
        if path and os.path.exists(path):
            with open(path) as f:
                a["instructions"] = f.read()
    return spec


def build_contract(spec: dict) -> DeclaredContract:
    """Build a `DeclaredContract` from an `AgenticArchitecture` spec dict.

    Same shape whether the dict comes from a Kubernetes CR (`.spec`) or an off-cluster ASL YAML —
    one schema, two delivery paths (E11 design: Docs/e11-configure-crd-design.md).
    """
    risk_map: dict[str, int] = {}
    for t in spec.get("tools", []) or []:
        name = t.get("name")
        if name:
            if name in risk_map:  # a duplicate would silently overwrite → reject (hash/audit)
                raise ValueError(f"duplicate tool {name!r} in the tools catalogue")
            risk_map[name] = int(t.get("risk", 0) or 0)

    agents: dict[str, AgentContract] = {}
    for a in spec.get("agents", []) or []:
        name = a.get("name")
        if not name:
            continue
        if name in agents:  # a duplicate agent would silently win → reject (contract integrity)
            raise ValueError(f"duplicate agent {name!r}")
        a_llm = a.get("llm") or {}                       # per-agent LLM override (E13 §Configurable LLM)
        agents[name] = AgentContract(
            name=name,
            allowed_tools=frozenset(a.get("tools", []) or []),
            scope=frozenset(a.get("scope", []) or []),
            can_delegate_to=frozenset(a.get("canDelegateTo", []) or []),
            reports_to=a.get("reportsTo"),
            tier=a.get("tier", "") or "",
            role=a.get("role", "") or "",
            model=a_llm.get("model") or a.get("model", "") or "",   # llm.model ?? model (shorthand)
            instructions=a.get("instructions", "") or "",
            llm_provider=a_llm.get("provider", "") or "",
            llm_endpoint=a_llm.get("endpoint", "") or "",
            # whole-backend binding (string = all tools, or {name, allow, deny} for least-privilege)
            mcp_backends=tuple(_parse_backend(b) for b in (a.get("mcpServers", []) or [])),
        )

    # Top-level `delegations: [{from, to}]` (E13 sugar) folds into the source agent's
    # can_delegate_to, so the graph can be declared edge-list-style or inline per agent.
    for d in spec.get("delegations", []) or []:
        src, dst = d.get("from"), d.get("to")
        if src and dst:
            if src not in agents:
                raise ValueError(f"delegation from unknown agent {src!r}")
            if dst not in agents:
                raise ValueError(f"delegation to unknown agent {dst!r}")
            agents[src] = replace(agents[src], can_delegate_to=agents[src].can_delegate_to | {dst})

    deny_sequences = tuple(
        DenyRule(sequence=tuple(r.get("deny", []) or []),
                 reason=r.get("reason", "") or "", agent=r.get("agent"))
        for r in (spec.get("rules", []) or []) if r.get("deny")
    )
    # observability.otel.attributes (E13 §4b) — telemetry attribute allow-list (none / * / list)
    otel = ((spec.get("observability") or {}).get("otel") or {})
    emit_attributes = tuple(otel.get("attributes", []) or [])

    # mcpServers (E13 §External tools) — external MCP tool sources, namespaced at import
    mcp_servers = tuple(
        # governed defaults to (not namespace) — a passthrough proxy is governed — but is explicit
        (s["name"], s["url"], s.get("namespace", True),
         s.get("governed", not s.get("namespace", True)))
        for s in (spec.get("mcpServers", []) or [])
        if s.get("name") and s.get("url")
    )

    # global LLM default (E13 §Configurable LLM) — agents override per field, env is the runtime floor
    g_llm = spec.get("llm") or {}

    return DeclaredContract(agents=agents, risk_map=risk_map,
                            topology=spec.get("topology", "") or "", deny_sequences=deny_sequences,
                            emit_attributes=emit_attributes, mcp_servers=mcp_servers,
                            llm_provider=g_llm.get("provider", "") or "",
                            llm_model=g_llm.get("model", "") or "",
                            llm_endpoint=g_llm.get("endpoint", "") or "")
