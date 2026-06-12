"""E11 — declared contract (configure/declare layer) unit tests.

The declared check is deterministic and independent of the statistical baseline: a call to an
unbound tool or out-of-scope is a declared violation; an agent absent from the contract is
unconstrained (DriftWatch-standalone degrades cleanly).
"""
from __future__ import annotations

import pytest

from agentgate.library.contract import (
    AgentContract,
    DeclaredContract,
    build_contract,
)

# A canonical AgenticArchitecture spec (CR .spec or off-cluster ASL YAML — same shape).
SPEC = {
    "tools": [
        {"name": "pods_list", "category": "k8s", "risk": 1},
        {"name": "pods_delete", "category": "k8s", "risk": 4},
        {"name": "namespaces_list", "category": "k8s", "risk": 1},
    ],
    "agents": [
        {
            "name": "ops-orchestrator",
            "tier": "strategic",
            "tools": ["namespaces_list"],
            "scope": ["ns:agents"],
            "canDelegateTo": ["pod-reaper"],
        },
        {
            "name": "pod-reaper",
            "tier": "execution",
            "reportsTo": "ops-orchestrator",
            "tools": ["pods_list", "pods_delete"],
            "scope": ["ns:demo"],
        },
    ],
    "topology": "pyramid",
}


def test_build_contract_parses_catalogue_and_agents():
    c = build_contract(SPEC)
    assert c.topology == "pyramid"
    assert c.risk_map == {"pods_list": 1, "pods_delete": 4, "namespaces_list": 1}
    assert set(c.agents) == {"ops-orchestrator", "pod-reaper"}
    reaper = c.agents["pod-reaper"]
    assert reaper.allowed_tools == frozenset({"pods_list", "pods_delete"})
    assert reaper.scope == frozenset({"ns:demo"})
    assert reaper.reports_to == "ops-orchestrator"
    assert c.agents["ops-orchestrator"].can_delegate_to == frozenset({"pod-reaper"})


def test_duplicate_agent_or_tool_is_rejected():  # consultant #4
    with pytest.raises(ValueError, match="duplicate agent"):
        build_contract({"agents": [{"name": "a"}, {"name": "a"}]})
    with pytest.raises(ValueError, match="duplicate tool"):
        build_contract({"tools": [{"name": "t"}, {"name": "t"}], "agents": [{"name": "a"}]})


def test_within_contract_call_passes():
    c = build_contract(SPEC)
    # pod-reaper bound to pods_delete in ns:demo → within contract
    assert c.check("pod-reaper", "pods_delete", "ns:demo") is None
    # nested scope is allowed (ns:demo/pod-x ⊆ ns:demo)
    assert c.check("pod-reaper", "pods_list", "ns:demo/pod-x") is None


def test_unbound_tool_is_declared_violation():
    c = build_contract(SPEC)
    # ops-orchestrator is bound only to namespaces_list → pods_delete is unbound
    reason = c.check("ops-orchestrator", "pods_delete", "ns:agents")
    assert reason is not None and "not bound" in reason


def test_out_of_scope_is_declared_violation():
    c = build_contract(SPEC)
    # pod-reaper may use pods_delete, but only in ns:demo — ns:prod is out of scope
    reason = c.check("pod-reaper", "pods_delete", "ns:prod")
    assert reason is not None and "scope" in reason


def test_unknown_agent_is_unconstrained_standalone():
    c = build_contract(SPEC)
    # an agent not in the contract has no declared constraint → engine == E1–E10 (standalone)
    assert c.check("some-other-agent", "pods_delete", "ns:prod") is None


def test_empty_bindings_or_scope_are_unconstrained():
    c = DeclaredContract(agents={"a": AgentContract(name="a")})  # no tools/scope declared
    assert c.check("a", "anything", "ns:wherever") is None


def test_delegation_allowed_reflects_declared_edges():
    c = build_contract(SPEC)
    assert c.delegation_allowed("ops-orchestrator", "pod-reaper") is True
    assert c.delegation_allowed("ops-orchestrator", "stranger") is False
    assert c.delegation_allowed("pod-reaper", "ops-orchestrator") is False  # no up-edge declared


def test_contract_hash_is_stable_and_order_independent():
    c1 = build_contract(SPEC)
    # reorder agents + tools → same logical contract → same hash
    spec2 = {
        "tools": list(reversed(SPEC["tools"])),
        "agents": list(reversed(SPEC["agents"])),
        "topology": "pyramid",
    }
    c2 = build_contract(spec2)
    assert c1.hash == c2.hash
    # a real change (drop a binding) → different hash
    spec3 = {**SPEC, "agents": [{**SPEC["agents"][1], "tools": ["pods_list"]}, SPEC["agents"][0]]}
    assert build_contract(spec3).hash != c1.hash


def test_empty_spec_builds_empty_contract():
    c = build_contract({})
    assert c.agents == {} and c.risk_map == {}
    assert c.check("any", "any", "any") is None  # nothing declared → unconstrained


# --- E11 persistence: operator writes, interceptor reads (consultant #2 chain) ---

def test_to_dict_from_dict_round_trip():
    from agentgate.library.contract import DeclaredContract
    c = build_contract(SPEC)
    c2 = DeclaredContract.from_dict(c.to_dict())
    assert c2.hash == c.hash
    assert c2.check("pod-reaper", "pods_delete", "ns:prod") is not None      # still enforced
    assert c2.check("pod-reaper", "pods_delete", "ns:demo") is None
    assert c2.risk_map == c.risk_map and c2.topology == c.topology


def test_save_and_load_contract(tmp_path):
    from agentgate.library.contract import load_contract, save_contract
    c = build_contract(SPEC)
    path = save_contract(c, str(tmp_path), "acme-org")
    assert path.exists() and path.name == "acme-org.json"
    loaded = load_contract(str(tmp_path), "acme-org")
    assert loaded is not None and loaded.hash == c.hash


def test_load_missing_contract_returns_none(tmp_path):
    from agentgate.library.contract import load_contract
    # absent ref → None → standalone-safe (policy degrades to statistical drift)
    assert load_contract(str(tmp_path), "nope") is None


def test_delete_contract(tmp_path):
    from agentgate.library.contract import delete_contract, load_contract, save_contract
    save_contract(build_contract(SPEC), str(tmp_path), "acme-org")
    delete_contract(str(tmp_path), "acme-org")
    assert load_contract(str(tmp_path), "acme-org") is None
    delete_contract(str(tmp_path), "acme-org")  # idempotent — no error if already gone


# --- E12 declared chain-rules (deny sequences) ---

SPEC_RULES = {
    **SPEC,
    "rules": [
        {"deny": ["pods_list", "pods_delete"], "reason": "list-then-delete is destructive recon"},
        {"deny": ["secrets_list", "pods_exec"], "agent": "pod-reaper", "reason": "exfil-shaped"},
    ],
}


def test_build_parses_deny_sequences():
    c = build_contract(SPEC_RULES)
    assert len(c.deny_sequences) == 2
    assert c.deny_sequences[0].sequence == ("pods_list", "pods_delete")
    assert c.deny_sequences[1].agent == "pod-reaper"


def test_deny_sequence_tail_match_blocks():  # D1 contiguous suffix
    c = build_contract(SPEC_RULES)
    # chain tail = [..., pods_list, pods_delete] -> fires
    r = c.check_sequence("pod-reaper", ["namespaces_list", "pods_list", "pods_delete"])
    assert r is not None and "deny-sequence" in r and "destructive recon" in r


def test_deny_sequence_non_tail_does_not_fire():  # only the tail matters
    c = build_contract(SPEC_RULES)
    # pods_list, pods_delete occur but NOT at the tail (a read follows) -> no match
    assert c.check_sequence("pod-reaper", ["pods_list", "pods_delete", "namespaces_list"]) is None
    # single non-final call of the pair -> no match
    assert c.check_sequence("pod-reaper", ["pods_list"]) is None


def test_deny_sequence_per_agent_scope():  # D4
    c = build_contract(SPEC_RULES)
    seq = ["secrets_list", "pods_exec"]
    assert c.check_sequence("pod-reaper", seq) is not None       # rule scoped to pod-reaper
    assert c.check_sequence("other-agent", seq) is None          # not for other agents
    # the org-wide rule (no agent) applies to anyone
    assert c.check_sequence("other-agent", ["pods_list", "pods_delete"]) is not None


def test_deny_sequences_survive_serialization():
    from agentgate.library.contract import DeclaredContract
    c = build_contract(SPEC_RULES)
    c2 = DeclaredContract.from_dict(c.to_dict())
    assert c2.hash == c.hash
    assert c2.check_sequence("pod-reaper", ["pods_list", "pods_delete"]) is not None


def test_no_rules_no_sequence_violation():
    c = build_contract(SPEC)  # SPEC has no rules
    assert c.deny_sequences == ()
    assert c.check_sequence("pod-reaper", ["pods_list", "pods_delete"]) is None


# --- E13 generation fields (instructions/model/role) + top-level delegations ---

SPEC_E13 = {
    "agents": [
        {"name": "planner", "role": "plan steps", "model": "gpt-4o",
         "instructions": "You are a planner. Produce ordered steps.", "tools": ["search"]},
        {"name": "coder", "model": "gpt-4o", "instructions": "Implement the step.",
         "tools": ["write_file"]},
        {"name": "reviewer", "instructions": "Review the code.", "tools": ["read_file"]},
    ],
    "delegations": [
        {"from": "planner", "to": "coder"},
        {"from": "coder", "to": "reviewer"},
    ],
}


def test_e13_generation_fields_parse():
    c = build_contract(SPEC_E13)
    p = c.agents["planner"]
    assert p.role == "plan steps"
    assert p.model == "gpt-4o"
    assert p.instructions.startswith("You are a planner")
    # instructions/model are opaque to the declared-check — they don't constrain a call
    assert c.check("planner", "search") is None


def test_e13_top_level_delegations_fold_into_can_delegate_to():
    c = build_contract(SPEC_E13)
    assert c.delegation_allowed("planner", "coder")
    assert c.delegation_allowed("coder", "reviewer")
    assert not c.delegation_allowed("reviewer", "coder")   # not declared


def test_e13_delegation_to_unknown_agent_rejected():
    with pytest.raises(ValueError, match="unknown agent"):
        build_contract({"agents": [{"name": "a"}], "delegations": [{"from": "a", "to": "ghost"}]})


def test_e13_fields_survive_serialization():
    c = build_contract(SPEC_E13)
    c2 = DeclaredContract.from_dict(c.to_dict())
    assert c2.hash == c.hash
    assert c2.agents["planner"].instructions == c.agents["planner"].instructions
    assert c2.delegation_allowed("planner", "coder")


def test_e13_fields_omitted_keep_e11_contract_byte_stable():
    # an E11/E12 contract (no role/model/instructions) must serialize exactly as before, so existing
    # contractHashes / e2e fixtures are unaffected by the additive E13 fields.
    c = build_contract(SPEC)
    d = c.to_dict()
    for a in d["agents"].values():
        assert "role" not in a and "model" not in a and "instructions" not in a


# --- E13 runtime delegation scorer (check_delegation) — for dynamic/conditional graphs ---

def test_check_delegation_allows_declared_edge():
    c = build_contract(SPEC_E13)                       # planner→coder→reviewer
    assert c.check_delegation("planner", "coder") is None


def test_check_delegation_flags_novel_edge():
    c = build_contract(SPEC_E13)
    reason = c.check_delegation("planner", "reviewer")  # not declared (planner→coder only)
    assert reason and "novel edge" in reason


def test_check_delegation_flags_cycle_on_active_path():
    # a contract whose declared graph has a 2-cycle (build_contract allows it; only codegen rejects)
    c = build_contract({"agents": [{"name": "a"}, {"name": "b"}],
                        "delegations": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]})
    assert c.check_delegation("a", "b", active_path=("a",)) is None     # first hop fine
    reason = c.check_delegation("b", "a", active_path=("a", "b"))       # a is already active
    assert reason and "cycle" in reason


def test_check_delegation_flags_scope_escalation():
    c = build_contract({"agents": [{"name": "p", "scope": ["ns:acme"]},
                                   {"name": "q", "scope": ["ns:other"]}],
                        "delegations": [{"from": "p", "to": "q"}]})
    reason = c.check_delegation("p", "q")              # ns:other ⊄ ns:acme
    assert reason and "escalates scope" in reason


def test_check_delegation_unknown_src_is_unconstrained():
    c = build_contract(SPEC_E13)
    assert c.check_delegation("ghost", "coder") is None   # standalone-safe


# --- E13 §4b: observability.otel.attributes (telemetry allow-list) ---

def test_emit_attributes_default_empty():
    assert build_contract({"agents": [{"name": "a"}]}).emit_attributes == ()


def test_emit_attributes_parsed_from_observability():
    c = build_contract({"agents": [{"name": "a"}],
                        "observability": {"otel": {"attributes": ["gen_ai.agent.id", "none"]}}})
    assert c.emit_attributes == ("gen_ai.agent.id", "none")


def test_emit_attributes_survive_serialization():
    c = build_contract({"agents": [{"name": "a"}],
                        "observability": {"otel": {"attributes": ["*"]}}})
    c2 = DeclaredContract.from_dict(c.to_dict())
    assert c2.emit_attributes == ("*",)
    assert c2.hash == c.hash


def test_emit_attributes_omitted_keep_contract_byte_stable():
    c = build_contract(SPEC)  # no observability
    assert "emit_attributes" not in c.to_dict()


# --- E13 §External tools: mcpServers ---

def test_mcp_servers_parsed_and_serialized():
    spec = {"agents": [{"name": "a"}],
            "mcpServers": [{"name": "k8s", "url": "http://proxy:8000/mcp"}]}
    c = build_contract(spec)
    # (name, url, namespace, governed) — namespace defaults True → governed defaults (not True) = False
    assert c.mcp_servers == (("k8s", "http://proxy:8000/mcp", True, False),)
    c2 = DeclaredContract.from_dict(c.to_dict())
    assert c2.mcp_servers == c.mcp_servers and c2.hash == c.hash


def test_mcp_servers_namespace_passthrough():
    # namespace: false (proxy already namespaced) → governed defaults True; round-trips
    spec = {"agents": [{"name": "a"}],
            "mcpServers": [{"name": "k8s", "url": "http://proxy:8000/mcp", "namespace": False}]}
    c = build_contract(spec)
    assert c.mcp_servers == (("k8s", "http://proxy:8000/mcp", False, True),)
    c2 = DeclaredContract.from_dict(c.to_dict())
    assert c2.mcp_servers == c.mcp_servers and c2.hash == c.hash


def test_mcp_servers_explicit_governed_decoupled_from_namespace():
    # consultant #4: governed is explicit, independent of namespace (no fragile coupling)
    spec = {"agents": [{"name": "a"}], "mcpServers": [
        {"name": "p", "url": "http://proxy/mcp", "namespace": True, "governed": True},    # proxy, prefixed
        {"name": "d", "url": "http://direct/mcp", "namespace": False, "governed": False},  # not governed
    ]}
    c = build_contract(spec)
    assert c.mcp_servers == (("p", "http://proxy/mcp", True, True),
                             ("d", "http://direct/mcp", False, False))


def test_mcp_servers_default_empty_and_byte_stable():
    c = build_contract(SPEC)
    assert c.mcp_servers == ()
    assert "mcp_servers" not in c.to_dict()


# --- E13 §Configurable LLM: global + per-agent + instruction sourcing ---

def test_global_llm_default_applies_to_agents():
    c = build_contract({"llm": {"provider": "ollama", "model": "qwen3.5:9b",
                                "endpoint": "http://h:11434"},
                        "agents": [{"name": "a"}]})
    assert c.effective_llm("a") == ("ollama", "qwen3.5:9b", "http://h:11434")


def test_per_agent_llm_overrides_global_field_by_field():
    c = build_contract({"llm": {"provider": "ollama", "model": "qwen3.5:9b", "endpoint": "http://h:11434"},
                        "agents": [{"name": "a", "llm": {"model": "gpt-4o", "provider": "openai"}}]})
    # model + provider overridden; endpoint falls back to global
    assert c.effective_llm("a") == ("openai", "gpt-4o", "http://h:11434")


def test_agent_model_shorthand_still_works():
    c = build_contract({"agents": [{"name": "a", "model": "qwen3.5:9b"}]})
    assert c.effective_llm("a") == ("", "qwen3.5:9b", "")   # provider/endpoint → env floor at runtime


def test_llm_survives_serialization_and_byte_stable_when_absent():
    c = build_contract({"llm": {"model": "m"}, "agents": [{"name": "a"}]})
    c2 = DeclaredContract.from_dict(c.to_dict())
    assert c2.effective_llm("a") == ("", "m", "") and c2.hash == c.hash
    assert "llm" not in build_contract(SPEC).to_dict()      # no llm → byte-stable


def test_instructions_from_path(tmp_path):
    from agentgate.library.contract import resolve_instructions
    p = tmp_path / "coder.md"
    p.write_text("You are the coder.")
    spec = {"agents": [{"name": "coder", "instructionsFrom": {"path": str(p)}}]}
    resolve_instructions(spec)
    assert build_contract(spec).agents["coder"].instructions == "You are the coder."


def test_inline_instructions_win_over_from(tmp_path):
    from agentgate.library.contract import resolve_instructions
    p = tmp_path / "x.md"
    p.write_text("FROM FILE")
    spec = {"agents": [{"name": "a", "instructions": "INLINE",
                        "instructionsFrom": {"path": str(p)}}]}
    resolve_instructions(spec)
    assert spec["agents"][0]["instructions"] == "INLINE"


def test_check_delegation_strict_treats_unknown_as_violation():
    # consultant #3: external-event scorer — unknown src/dst is a violation (zero-trust); the in-pod
    # non-strict path keeps the standalone-safe None for an unknown src.
    c = build_contract({"agents": [{"name": "a"}, {"name": "b"}],
                        "delegations": [{"from": "a", "to": "b"}]})
    assert c.check_delegation("a", "b") is None                 # declared edge
    assert c.check_delegation("ghost", "b") is None             # non-strict: unknown src → unconstrained
    assert c.check_delegation("ghost", "b", strict=True)        # strict: unknown src → violation
    assert c.check_delegation("a", "ghost", strict=True)        # strict: unknown dst → violation


def test_govern_proxytype_parsed_and_serialized():
    spec = {"agents": [{"name": "a"}],
            "govern": {"proxyType": "driftwatch", "endpoint": "http://dw:8000/mcp",
                       "register": "http://dw:8080/contracts"}}
    c = build_contract(spec)
    assert c.govern["proxyType"] == "driftwatch"
    c2 = DeclaredContract.from_dict(c.to_dict())
    assert c2.govern == c.govern and c2.hash == c.hash


def test_govern_empty_byte_stable():
    c = build_contract({"agents": [{"name": "a"}]})
    assert c.govern == {}
    assert "govern" not in c.to_dict()
