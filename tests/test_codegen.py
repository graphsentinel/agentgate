"""E13 codegen — LangGraph generator (Docs/e13-mabac-delegation-design.md §B).

Creation-driven governance is the key property under test: only declared delegation edges may
appear in the generated graph, so a forbidden hand-off is impossible by construction.
"""
from __future__ import annotations

import ast

import pytest

from agentgate.codegen import (
    coordinator,
    find_cycle,
    generate_langgraph,
    make_agent_node,
    validate_for_generation,
)
from agentgate.library.contract import build_contract

SPEC = {
    "agents": [
        {"name": "planner", "tier": "strategic", "model": "gpt-4o",
         "instructions": "You are a planner. Produce ordered steps.", "tools": ["search"]},
        {"name": "coder", "model": "gpt-4o",
         "instructions": "Implement the step.", "tools": ["write_file", "run_tests"]},
        {"name": "reviewer", "instructions": "Review the code.", "tools": ["read_file"]},
    ],
    "delegations": [
        {"from": "planner", "to": "coder"},
        {"from": "coder", "to": "reviewer"},
    ],
}


def test_coordinator_is_the_agent_with_no_incoming_edge():
    assert coordinator(build_contract(SPEC)) == "planner"


def test_generated_module_is_syntactically_valid():
    src = generate_langgraph(build_contract(SPEC))
    ast.parse(src)  # raises SyntaxError on bad codegen


def test_generated_graph_has_only_declared_edges_and_entry():
    src = generate_langgraph(build_contract(SPEC))
    assert "g.add_edge('planner', 'coder')" in src
    assert "g.add_edge('coder', 'reviewer')" in src
    assert "g.add_edge('reviewer', END)" in src        # leaf terminates
    assert "g.set_entry_point('planner')" in src       # coordinator entry


def test_creation_driven_undeclared_edge_is_never_generated():
    # reviewer -> coder is NOT declared, so it must not appear in the generated graph at all.
    src = generate_langgraph(build_contract(SPEC))
    assert "g.add_edge('reviewer', 'coder')" not in src
    assert "'reviewer', 'coder'" not in src


def test_instructions_model_tools_are_wired_into_nodes():
    src = generate_langgraph(build_contract(SPEC))
    assert "You are a planner" in src
    assert "'gpt-4o'" in src
    assert "'write_file'" in src and "'run_tests'" in src


def test_entry_override():
    src = generate_langgraph(build_contract(SPEC), entry="coder")
    assert "g.set_entry_point('coder')" in src


def test_no_agents_rejected():
    with pytest.raises(ValueError, match="no agents"):
        generate_langgraph(build_contract({"agents": []}))


# --- creation-driven validation (phase 3): generate-time DAG + scope monotonicity ---

def test_acyclic_scope_monotonic_graph_validates():
    scoped = {
        "agents": [
            {"name": "parent", "scope": ["ns:acme"]},
            {"name": "child", "scope": ["ns:acme/team"]},     # ⊆ parent
        ],
        "delegations": [{"from": "parent", "to": "child"}],
    }
    validate_for_generation(build_contract(scoped))           # no raise
    assert "g.add_edge('parent', 'child')" in generate_langgraph(build_contract(scoped))


def test_cyclic_graph_fails_to_generate():
    cyclic = {"agents": [{"name": "a"}, {"name": "b"}],
              "delegations": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]}
    c = build_contract(cyclic)
    assert find_cycle(c) is not None
    with pytest.raises(ValueError, match="cycle"):
        generate_langgraph(c)


def test_self_edge_is_a_cycle():
    c = build_contract({"agents": [{"name": "a"}], "delegations": [{"from": "a", "to": "a"}]})
    with pytest.raises(ValueError, match="cycle"):
        generate_langgraph(c)


def test_scope_escalation_fails_to_generate():
    escalating = {
        "agents": [
            {"name": "parent", "scope": ["ns:acme"]},
            {"name": "child", "scope": ["ns:other"]},         # ⊄ parent → widening
        ],
        "delegations": [{"from": "parent", "to": "child"}],
    }
    with pytest.raises(ValueError, match="scope escalation"):
        generate_langgraph(build_contract(escalating))


def test_unconstrained_child_under_constrained_parent_is_escalation():
    esc = {
        "agents": [
            {"name": "parent", "scope": ["ns:acme"]},
            {"name": "child"},                                 # no scope = unconstrained > parent
        ],
        "delegations": [{"from": "parent", "to": "child"}],
    }
    with pytest.raises(ValueError, match="scope escalation"):
        generate_langgraph(build_contract(esc))


def test_unconstrained_parent_allows_any_child_scope():
    ok = {
        "agents": [
            {"name": "parent"},                                # unconstrained parent
            {"name": "child", "scope": ["ns:acme"]},
        ],
        "delegations": [{"from": "parent", "to": "child"}],
    }
    validate_for_generation(build_contract(ok))                # no raise


# --- execute layer (phase 4): runtime node + the generated graph actually runs ---

def test_make_agent_node_records_run_without_an_llm():
    node = make_agent_node(name="planner", model="gpt-4o", instructions="plan", tools=["search"])
    out = node({"history": []})
    assert out["last"] == "planner"
    assert out["history"][-1] == {"agent": "planner", "model": "gpt-4o", "tools": ["search"]}


def test_generated_graph_compiles_and_runs_from_the_coordinator():
    pytest.importorskip("langgraph")  # generated app needs the framework; codegen[/extra]
    src = generate_langgraph(build_contract(SPEC))
    ns: dict = {}
    exec(compile(src, "<generated>", "exec"), ns)   # noqa: S102 — exercising generated code under test
    graph = ns["build_graph"]()
    result = graph.invoke({"history": []})
    ran = [h["agent"] for h in result["history"]]
    assert ran[0] == "planner"        # commanded via the coordinator (entry)
    assert "coder" in ran and "reviewer" in ran   # flowed down the declared graph to the leaf


# --- CLI: agentgate generate / run from a YAML file ---

def _write_yaml(tmp_path):
    import yaml
    spec = tmp_path / "org.yaml"
    spec.write_text(yaml.safe_dump({"spec": {
        "agents": [{"name": "a", "model": "m", "instructions": "plan"}, {"name": "b"}],
        "delegations": [{"from": "a", "to": "b"}],
    }}))
    return spec


def test_cli_generate_emits_app_from_yaml(tmp_path, capsys):
    from agentgate.cli import main
    assert main(["generate", str(_write_yaml(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "def build_graph" in out
    assert "g.set_entry_point('a')" in out      # coordinator = a (no incoming edge)
    assert "g.add_edge('a', 'b')" in out


def test_cli_generate_writes_file(tmp_path):
    from agentgate.cli import main
    out = tmp_path / "app.py"
    assert main(["generate", str(_write_yaml(tmp_path)), "-o", str(out)]) == 0
    ast.parse(out.read_text())                  # the written module is valid Python


def test_cli_run_stub(tmp_path, capsys):
    pytest.importorskip("langgraph")
    from agentgate.cli import main
    assert main(["run", str(_write_yaml(tmp_path)), "--goal", "do a thing"]) == 0
    out = capsys.readouterr().out
    assert "a" in out and "b" in out            # both agents ran (stub, no LLM env)


# --- tool binding (execute layer): registry + creation-driven bound-only + tool loop ---

class _FakeResp:
    def __init__(self, message):
        self._m = message

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": self._m}


def test_calculator_tool_runs_and_is_sandboxed():
    from agentgate.codegen.tools import calculator
    assert calculator("2 * (3 + 4)") == "14"
    assert calculator("__import__('os')").startswith("error")   # no names/calls allowed


def test_only_bound_tools_are_offered_to_the_model(monkeypatch):
    captured: dict = {}

    def fake_post(url, json, timeout):
        captured["payload"] = json
        return _FakeResp({"content": "done"})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("DRIFTWATCH_LLM_PROVIDER", "ollama")
    node = make_agent_node(name="a", model="m", instructions="i", tools=["calculator"])
    node({"goal": "x", "history": []})
    offered = [t["function"]["name"] for t in captured["payload"].get("tools", [])]
    assert offered == ["calculator"]            # creation-driven: only the bound tool is exposed


def test_unbound_tool_offers_no_tools(monkeypatch):
    captured: dict = {}

    def fake_post(url, json, timeout):
        captured["payload"] = json
        return _FakeResp({"content": "done"})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("DRIFTWATCH_LLM_PROVIDER", "ollama")
    make_agent_node(name="a", model="m", instructions="i", tools=["nonexistent"])(
        {"goal": "x", "history": []})
    assert "tools" not in captured["payload"]   # unknown tool resolves to nothing offered


def test_tool_loop_executes_bound_tool_and_feeds_result_back(monkeypatch):
    turns: list[dict] = []

    def fake_post(url, json, timeout):
        turns.append(json)
        if len(turns) == 1:                      # first turn: model asks for the calculator
            return _FakeResp({"role": "assistant", "tool_calls": [
                {"function": {"name": "calculator", "arguments": {"expression": "2+3"}}}]})
        return _FakeResp({"content": "the answer is 5"})   # second turn: final answer

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("DRIFTWATCH_LLM_PROVIDER", "ollama")
    out = make_agent_node(name="a", model="m", instructions="i", tools=["calculator"])(
        {"goal": "compute 2+3", "history": []})
    # the tool result (5) was fed back to the model on the second turn
    assert any(m.get("role") == "tool" and "5" in m.get("content", "")
               for m in turns[1]["messages"])
    assert out["history"][-1]["output"] == "the answer is 5"
    # observability: the tool call is recorded in the run trace, marked allowed
    assert out["history"][-1]["tool_calls"] == [{"tool": "calculator", "ok": True}]


def test_tool_loop_refuses_an_unbound_tool_call(monkeypatch):
    turns: list[dict] = []

    def fake_post(url, json, timeout):
        turns.append(json)
        if len(turns) == 1:                      # model tries a tool it was NOT granted
            return _FakeResp({"role": "assistant", "tool_calls": [
                {"function": {"name": "calculator", "arguments": {"expression": "2+3"}}}]})
        return _FakeResp({"content": "ok"})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("DRIFTWATCH_LLM_PROVIDER", "ollama")
    # agent bound to read_file only — a calculator call must be refused at run time
    out = make_agent_node(name="a", model="m", instructions="i", tools=["read_file"])(
        {"goal": "x", "history": []})
    tool_msgs = [m for m in turns[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "not bound" in tool_msgs[0]["content"]
    # observability: the refused call is recorded in the trace, marked not-allowed
    assert out["history"][-1]["tool_calls"] == [{"tool": "calculator", "ok": False}]


# --- AutoGen target: same YAML, second framework (framework-agnostic) ---

def test_autogen_generates_valid_module_with_declared_transitions():
    from agentgate.codegen import generate, generate_autogen
    c = build_contract(SPEC)
    src = generate_autogen(c)
    ast.parse(src)                                       # valid Python
    assert "from autogen import" in src
    assert "speaker_transitions_type='allowed'" in src
    assert "planner: [coder]" in src                     # declared transition
    assert "coder: [reviewer]" in src
    assert "return manager, planner" in src              # coordinator = initial speaker
    assert generate(c, "autogen") == src                 # dispatch matches direct call


def test_autogen_is_creation_driven_no_undeclared_transition():
    from agentgate.codegen import generate_autogen
    src = generate_autogen(build_contract(SPEC))
    assert "reviewer: []" in src                         # leaf delegates to no one
    assert "reviewer: [coder]" not in src                # undeclared edge never generated


def test_autogen_shares_the_dag_scope_guard():
    from agentgate.codegen import generate_autogen
    cyclic = {"agents": [{"name": "a"}, {"name": "b"}],
              "delegations": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]}
    with pytest.raises(ValueError, match="cycle"):
        generate_autogen(build_contract(cyclic))


def test_same_yaml_generates_both_frameworks():
    from agentgate.codegen import generate
    c = build_contract(SPEC)
    lg, ag = generate(c, "langgraph"), generate(c, "autogen")
    ast.parse(lg)
    ast.parse(ag)
    assert "langgraph" in lg and "autogen" in ag         # one declaration, two runnable apps


def test_cli_generate_target_autogen(tmp_path, capsys):
    from agentgate.cli import main
    assert main(["generate", str(_write_yaml(tmp_path)), "--target", "autogen"]) == 0
    out = capsys.readouterr().out
    assert "from autogen import" in out and "build_group_chat" in out


# --- CrewAI target: same YAML, third framework ---

def test_crewai_generates_valid_module_in_topological_order():
    from agentgate.codegen import generate, generate_crewai
    c = build_contract(SPEC)
    src = generate_crewai(c)
    ast.parse(src)
    assert "from crewai import" in src
    assert "process=Process.sequential" in src
    # topological order: planner (coordinator) before coder before reviewer
    assert src.index("role='planner'") < src.index("role='coder'") < src.index("role='reviewer'")
    assert "tasks=[t_planner, t_coder, t_reviewer]" in src
    assert generate(c, "crewai") == src


def test_crewai_allow_delegation_reflects_out_edges():
    from agentgate.codegen import generate_crewai
    # reviewer is a leaf (no out-edges) → allow_delegation=False; planner/coder → True
    src = generate_crewai(build_contract(SPEC))
    assert "allow_delegation=True" in src and "allow_delegation=False" in src
    # the reviewer Agent block (its own def line .. next blank line) must carry False
    rev_block = src.split("reviewer = Agent(", 1)[1].split("\n\n", 1)[0]
    assert "allow_delegation=False" in rev_block


def test_crewai_shares_the_dag_scope_guard():
    from agentgate.codegen import generate_crewai
    cyclic = {"agents": [{"name": "a"}, {"name": "b"}],
              "delegations": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]}
    with pytest.raises(ValueError, match="cycle"):
        generate_crewai(build_contract(cyclic))


def test_topological_order_roots_first():
    from agentgate.codegen import topological_order
    order = topological_order(build_contract(SPEC))
    assert order[0] == "planner" and order[-1] == "reviewer"


def test_same_yaml_generates_all_three_frameworks():
    from agentgate.codegen import generate
    c = build_contract(SPEC)
    for target, marker in [("langgraph", "langgraph"), ("autogen", "autogen"), ("crewai", "crewai")]:
        src = generate(c, target)
        ast.parse(src)
        assert marker in src


def test_cli_generate_target_crewai(tmp_path, capsys):
    from agentgate.cli import main
    assert main(["generate", str(_write_yaml(tmp_path)), "--target", "crewai"]) == 0
    assert "from crewai import" in capsys.readouterr().out


# --- phase 6b (2/2): dynamic generator + runtime delegation gate ---

def test_dynamic_generator_emits_gated_conditional_routing():
    src = generate_langgraph(build_contract(SPEC), dynamic=True)
    ast.parse(src)
    assert "DeclaredContract.from_dict" in src        # contract embedded for the runtime gate
    assert "add_conditional_edges" in src             # dynamic routing, not fixed edges
    assert "make_router" in src
    assert "can_delegate_to=['coder']" in src         # planner picks among declared targets
    assert "contract=_CONTRACT, delegation_action=_ACTION" in src


def test_dynamic_node_gates_an_undeclared_pick(monkeypatch):
    # the model (planner) picks reviewer, which is NOT declared for planner → blocked + recorded
    monkeypatch.setattr("httpx.post",
                        lambda url, json, timeout: _FakeResp({"content": "done\nNEXT: reviewer"}))
    monkeypatch.setenv("DRIFTWATCH_LLM_PROVIDER", "ollama")
    c = build_contract(SPEC)
    node = make_agent_node(name="planner", model="m", instructions="i",
                           can_delegate_to=["coder"], contract=c, delegation_action="block")
    out = node({"goal": "x", "history": []})
    assert out["next"] is None                        # undeclared hand-off dropped (→ END)
    assert out["violations"][0]["dst"] == "reviewer"
    assert "novel edge" in out["violations"][0]["reason"]
    assert out["history"][-1]["delegation_violation"]["dst"] == "reviewer"


def test_dynamic_node_allows_a_declared_pick(monkeypatch):
    monkeypatch.setattr("httpx.post",
                        lambda url, json, timeout: _FakeResp({"content": "ok\nNEXT: coder"}))
    monkeypatch.setenv("DRIFTWATCH_LLM_PROVIDER", "ollama")
    c = build_contract(SPEC)
    out = make_agent_node(name="planner", model="m", instructions="i",
                          can_delegate_to=["coder"], contract=c)({"goal": "x", "history": []})
    assert out["next"] == "coder" and not out.get("violations")


def test_dynamic_node_log_action_records_but_does_not_block(monkeypatch):
    monkeypatch.setattr("httpx.post",
                        lambda url, json, timeout: _FakeResp({"content": "x\nNEXT: reviewer"}))
    monkeypatch.setenv("DRIFTWATCH_LLM_PROVIDER", "ollama")
    c = build_contract(SPEC)
    out = make_agent_node(name="planner", model="m", instructions="i",
                          can_delegate_to=["coder"], contract=c, delegation_action="log")(
        {"goal": "x", "history": []})
    assert out["next"] == "reviewer"                  # log: proceeds anyway
    assert out["violations"][0]["reason"]             # but the violation is recorded


def test_dynamic_node_stub_picks_first_declared():
    from agentgate.codegen import make_router
    c = build_contract(SPEC)
    out = make_agent_node(name="planner", model="", instructions="i",
                          can_delegate_to=["coder"], contract=c)({"goal": "x", "history": []})
    assert out["next"] == "coder"                     # stub (no LLM) → first declared, allowed
    assert make_router()({"next": "coder"}) == "coder"
    assert make_router()({"next": None}) == "__end__"


def test_dynamic_graph_runs_clean_with_stub(monkeypatch):
    pytest.importorskip("langgraph")
    src = generate_langgraph(build_contract(SPEC), dynamic=True)
    ns: dict = {}
    exec(compile(src, "<dyn>", "exec"), ns)  # noqa: S102
    result = ns["build_graph"]().invoke({"goal": "x", "history": []})
    ran = [h["agent"] for h in result["history"]]
    assert ran[0] == "planner"                        # stub picks declared at each step → no violation
    assert not result.get("violations")


# --- observability: gen_ai.agent.* emit (no-op without an endpoint) ---

def test_emit_agent_run_builds_gen_ai_span_attrs():
    from agentgate.otel.emit import Emitter
    em = Emitter()  # no endpoint → pure dict builder, no exporter
    out = em.emit_agent_run(agent_id="planner", task_type="do x", model="qwen3.5:9b",
                            tools=["search"], tool_calls=[{"tool": "search", "ok": True}])
    assert out["span"]["gen_ai.agent.id"] == "planner"
    assert out["span"]["gen_ai.request.model"] == "qwen3.5:9b"
    assert out["span"]["gen_ai.agent.tools"] == ["search"]


def test_emit_agent_run_marks_delegation_violation():
    from agentgate.otel.emit import Emitter
    out = Emitter().emit_agent_run(agent_id="planner", task_type="x",
                                   violation={"dst": "reviewer", "reason": "novel edge"})
    s = out["span"]
    assert s["gen_ai.agent.gate.declared"] is True
    assert s["gen_ai.agent.computed.anomaly.kind"] == "delegation_violation"
    assert "novel edge" in s["gen_ai.agent.gate.reason"]


def test_agent_node_runs_without_endpoint_no_emit(monkeypatch):
    # no AGENTGATE_OTLP_ENDPOINT → _emitter() is None → node still runs (stub), emits nothing
    monkeypatch.delenv("AGENTGATE_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("DRIFTWATCH_OTLP_ENDPOINT", raising=False)
    import agentgate.codegen.runtime as rt
    rt._EMITTER = None
    rt._EMITTER_READY = False
    out = make_agent_node(name="planner", model="", instructions="i")({"goal": "x", "history": []})
    assert out["history"][-1]["agent"] == "planner"
    assert rt._emitter() is None


# --- E13 §4b: emit_agent_run attribute allow-list (none / * / list) ---

def test_emit_run_none_emits_nothing():
    from agentgate.otel.emit import Emitter
    out = Emitter().emit_agent_run(agent_id="a", task_type="x", model="m", attributes=["none"])
    assert out["emitted"] is False and out["span"] == {}


def test_emit_run_star_and_default_emit_all():
    from agentgate.otel.emit import Emitter
    for attrs in (["*"], None, ()):
        out = Emitter().emit_agent_run(agent_id="a", task_type="x", model="m", attributes=attrs)
        assert out["span"]["gen_ai.agent.id"] == "a"
        assert out["span"]["gen_ai.request.model"] == "m"


def test_emit_run_allowlist_filters_to_listed_only():
    from agentgate.otel.emit import Emitter
    out = Emitter().emit_agent_run(agent_id="a", task_type="x", model="m", tools=["t"],
                                   attributes=["gen_ai.agent.id"])
    assert out["span"] == {"gen_ai.agent.id": "a"}   # model/tools filtered out


def test_generated_node_carries_emit_attributes():
    from agentgate.codegen import generate_langgraph
    c = build_contract({"agents": [{"name": "a", "model": "m"}],
                        "observability": {"otel": {"attributes": ["gen_ai.agent.id"]}}})
    assert "emit_attributes=['gen_ai.agent.id']" in generate_langgraph(c)
    assert "emit_attributes=['gen_ai.agent.id']" in generate_langgraph(c, dynamic=True)


# --- E13 §External tools: dynamic MCP tool import (mocked FastMCP client) ---

class _FakeMCPTool:
    def __init__(self, name, desc="", schema=None):
        self.name = name
        self.description = desc
        self.inputSchema = schema or {"type": "object", "properties": {}}


class _FakeClient:
    """Async context manager mimicking fastmcp.Client for list_tools/call_tool."""
    def __init__(self, url): self.url = url
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def list_tools(self): return [_FakeMCPTool("pods_list"), _FakeMCPTool("pods_get")]
    async def call_tool(self, name, args): return f"{name}({args}) -> ok"


def _install_fake_fastmcp(monkeypatch):
    import sys
    import types
    mod = types.ModuleType("fastmcp")
    mod.Client = _FakeClient
    monkeypatch.setitem(sys.modules, "fastmcp", mod)


def test_register_mcp_tools_imports_namespaced(monkeypatch):
    from agentgate.codegen import bound_tools, register_mcp_tools
    from agentgate.codegen import tools as toolmod
    _install_fake_fastmcp(monkeypatch)
    names = register_mcp_tools("k8s", "http://proxy:8000/mcp")
    assert names == ["k8s_pods_list", "k8s_pods_get"]        # namespaced <server>_<tool>
    # the proxy callable forwards to the MCP server
    assert "pods_list" in toolmod.get_tool("k8s_pods_list").func(name="x")
    # creation-driven binding still applies: an agent bound to only one gets only one
    assert [t.name for t in bound_tools(["k8s_pods_list"])] == ["k8s_pods_list"]


def test_register_mcp_tools_unreachable_is_graceful(monkeypatch):
    from agentgate.codegen import register_mcp_tools
    # no fastmcp installed / connection error → [] (standalone-safe, run proceeds)
    import sys
    monkeypatch.setitem(sys.modules, "fastmcp", None)  # import fails inside
    assert register_mcp_tools("k8s", "http://nope:9999/mcp") == []


# --- E13 §Configurable LLM: generated node carries effective provider/endpoint ---

def test_generated_node_carries_effective_llm():
    from agentgate.codegen import generate_langgraph
    c = build_contract({"llm": {"provider": "ollama", "endpoint": "http://h:11434"},
                        "agents": [{"name": "a", "model": "qwen3.5:9b"}]})
    src = generate_langgraph(c)
    assert "llm_provider='ollama'" in src
    assert "llm_endpoint='http://h:11434'" in src
    assert "model='qwen3.5:9b'" in src


def test_per_agent_endpoint_override_in_generated_node():
    from agentgate.codegen import generate_langgraph
    c = build_contract({"llm": {"provider": "ollama", "endpoint": "http://global:11434"},
                        "agents": [{"name": "a", "model": "m",
                                    "llm": {"endpoint": "http://special:11434"}}]})
    assert "llm_endpoint='http://special:11434'" in generate_langgraph(c)


# --- E13 backend binding (whole-backend): agent.mcpServers → all of that backend's tools ---

def test_backend_binding_gives_all_backend_tools(monkeypatch):
    from agentgate.codegen import backend_tools, register_mcp_tools
    from agentgate.codegen import tools as toolmod
    # reset registry/backends
    monkeypatch.setattr(toolmod, "_BACKEND_TOOLS", {}, raising=False)
    _install_fake_fastmcp(monkeypatch)
    register_mcp_tools("k8s_gov", "http://proxy:8000/mcp")     # → k8s_gov_pods_list, k8s_gov_pods_get
    assert backend_tools(["k8s_gov"]) == ["k8s_gov_pods_list", "k8s_gov_pods_get"]


def test_backend_binding_node_offers_all_backend_tools(monkeypatch):
    # an agent bound to a whole backend (no explicit tools) is offered ALL of its tools at run time
    from agentgate.codegen import register_mcp_tools
    from agentgate.codegen import tools as toolmod
    monkeypatch.setattr(toolmod, "_BACKEND_TOOLS", {}, raising=False)
    _install_fake_fastmcp(monkeypatch)
    register_mcp_tools("k8s_gov", "http://proxy:8000/mcp")

    captured: dict = {}

    def fake_post(url, json, timeout):
        captured["payload"] = json
        return _FakeResp({"content": "done"})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("DRIFTWATCH_LLM_PROVIDER", "ollama")
    make_agent_node(name="ops", model="m", instructions="i", mcp_backends=[("k8s_gov", (), ())])(
        {"goal": "x", "history": []})
    offered = sorted(t["function"]["name"] for t in captured["payload"].get("tools", []))
    assert offered == ["k8s_gov_pods_get", "k8s_gov_pods_list"]   # whole backend, no per-tool list


def test_backend_binding_in_contract_and_generated_node():
    from agentgate.codegen import generate_langgraph
    c = build_contract({"agents": [{"name": "ops", "model": "m", "mcpServers": ["k8s_gov"]}]})
    assert c.agents["ops"].mcp_backends == (("k8s_gov", (), ()),)   # bare name → all tools
    assert "mcp_backends=[('k8s_gov', (), ())]" in generate_langgraph(c)


def test_backend_allowlist_filters_offered_tools(monkeypatch):
    # consultant allowlist: {name, allow, deny} narrows the whole-backend set (glob over namespaced)
    from agentgate.codegen import backend_tools_filtered, register_mcp_tools
    from agentgate.codegen import tools as toolmod
    monkeypatch.setattr(toolmod, "_BACKEND_TOOLS", {}, raising=False)
    _install_fake_fastmcp(monkeypatch)   # registers k8s_pods_list, k8s_pods_get
    register_mcp_tools("k8s", "http://proxy:8000/mcp", namespace=True)
    # bare backend → all
    assert backend_tools_filtered([("k8s", (), ())]) == ["k8s_pods_list", "k8s_pods_get"]
    # allow glob → only matches
    assert backend_tools_filtered([("k8s", ("*_pods_get",), ())]) == ["k8s_pods_get"]
    # deny removes
    assert backend_tools_filtered([("k8s", (), ("*_pods_get",))]) == ["k8s_pods_list"]


def test_backend_binding_parses_allow_deny_object():
    c = build_contract({"agents": [{"name": "ops", "mcpServers": [
        {"name": "k8s", "allow": ["pods_list"]}, "tekton", {"name": "ci", "deny": ["x"]}]}]})
    assert c.agents["ops"].mcp_backends == (
        ("k8s", ("pods_list",), ()), ("tekton", (), ()), ("ci", (), ("x",)))


def test_namespace_passthrough_keeps_server_tool_names(monkeypatch):
    # namespace=False (proxy already namespaced) → register verbatim, no <name>_ prefix → no double
    from agentgate.codegen import backend_tools, register_mcp_tools
    from agentgate.codegen import tools as toolmod
    monkeypatch.setattr(toolmod, "_BACKEND_TOOLS", {}, raising=False)
    _install_fake_fastmcp(monkeypatch)
    register_mcp_tools("k8s", "http://proxy:8000/mcp", namespace=False)
    assert backend_tools(["k8s"]) == ["pods_list", "pods_get"]            # verbatim, NOT k8s_pods_list
    register_mcp_tools("k8s2", "http://proxy:8000/mcp", namespace=True)
    assert backend_tools(["k8s2"]) == ["k8s2_pods_list", "k8s2_pods_get"]  # prefixed


def test_register_mcp_tools_degrades_on_failure(monkeypatch):
    # an unreachable/slow server registers nothing and the backend is empty — pod still starts
    import sys
    import types
    from agentgate.codegen import backend_tools, register_mcp_tools
    from agentgate.codegen import tools as toolmod

    class _BoomClient:
        def __init__(self, url): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def list_tools(self): raise TimeoutError("slow")

    mod = types.ModuleType("fastmcp")
    mod.Client = _BoomClient
    monkeypatch.setitem(sys.modules, "fastmcp", mod)
    monkeypatch.setattr(toolmod, "_BACKEND_TOOLS", {}, raising=False)
    assert register_mcp_tools("dead", "http://nope/mcp") == []
    assert backend_tools(["dead"]) == []


# --- chain grouping: one MCP session per agent-run → DriftWatch groups calls into one chain ---

class _CountingClient:
    """Counts how many MCP sessions (Client instances) get opened."""
    opened = 0

    def __init__(self, url):
        type(self).opened += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    last_meta = None

    async def call_tool(self, name, args, meta=None):
        type(self).last_meta = (args or {}).get("_meta") if isinstance(args, dict) else None
        return f"{name} ok"


def _install_counting_client(monkeypatch):
    import sys
    import types
    _CountingClient.opened = 0
    mod = types.ModuleType("fastmcp")
    mod.Client = _CountingClient
    monkeypatch.setitem(sys.modules, "fastmcp", mod)


def test_mcp_session_reuses_one_client_for_the_run(monkeypatch):
    # inside an McpSession, repeated calls to the same url reuse ONE Client (one MCP session), so the
    # DriftWatch proxy correlates them into a single chain (sequence drift + baseline)
    from agentgate.codegen import McpSession
    from agentgate.codegen import tools as toolmod
    _install_counting_client(monkeypatch)
    proxy = toolmod._mcp_proxy("http://proxy/mcp", "pods_list")
    with McpSession():
        assert "pods_list ok" in proxy()
        proxy()
        proxy()
    assert _CountingClient.opened == 1     # one session for the whole agent-run


def test_without_session_each_call_opens_its_own(monkeypatch):
    # no McpSession → per-call session (the proxy sees isolated chains) — the degraded baseline
    from agentgate.codegen import tools as toolmod
    _install_counting_client(monkeypatch)
    proxy = toolmod._mcp_proxy("http://proxy/mcp", "pods_list")
    proxy()
    proxy()
    assert _CountingClient.opened == 2     # fresh session per call


def test_mcp_session_sends_meta_only_to_governed_proxy_urls(monkeypatch):
    # §4c + consultant #3: the prompt rides _meta ONLY to governed (proxy, namespace=False) urls —
    # a direct server (governed kaydı yok) must NOT receive the prompt.
    from agentgate.codegen import McpSession
    from agentgate.codegen import tools as toolmod
    monkeypatch.setattr(toolmod, "_GOVERNED_URLS", set(), raising=False)
    _install_counting_client(monkeypatch)
    toolmod._GOVERNED_URLS.add("http://proxy/mcp")          # mark this url as governed (proxy)
    meta = {"agent": "ops", "task_type": "list pods", "prompt": "list the pods"}

    _CountingClient.last_meta = None
    with McpSession(meta=meta):
        toolmod._mcp_proxy("http://proxy/mcp", "pods_list")()      # governed → meta sent
    assert _CountingClient.last_meta == meta

    _CountingClient.last_meta = None
    with McpSession(meta=meta):
        toolmod._mcp_proxy("http://direct/mcp", "pods_list")()     # NOT governed → no prompt leak
    assert _CountingClient.last_meta is None


def test_mcp_session_fail_fast_on_meta_arg_collision(monkeypatch):
    # consultant #3: never silently overwrite a real tool argument named "_meta"
    import pytest
    from agentgate.codegen import McpSession
    from agentgate.codegen import tools as toolmod
    monkeypatch.setattr(toolmod, "_GOVERNED_URLS", {"http://proxy/mcp"}, raising=False)
    _install_counting_client(monkeypatch)
    sess = McpSession(meta={"prompt": "x"})
    with sess, pytest.raises(ValueError, match="_meta"):
        sess.call("http://proxy/mcp", "pods_list", {"_meta": "real-tool-arg"})


def test_register_marks_governed_url_when_passthrough(monkeypatch):
    # namespace=False (proxy passthrough) ⟹ url is governed ⟹ eligible for prompt _meta
    from agentgate.codegen import register_mcp_tools
    from agentgate.codegen import tools as toolmod
    monkeypatch.setattr(toolmod, "_GOVERNED_URLS", set(), raising=False)
    monkeypatch.setattr(toolmod, "_BACKEND_TOOLS", {}, raising=False)
    _install_fake_fastmcp(monkeypatch)
    register_mcp_tools("k8s", "http://proxy:8000/mcp", namespace=False)
    assert "http://proxy:8000/mcp" in toolmod._GOVERNED_URLS
    register_mcp_tools("cal", "http://direct:9000/mcp", namespace=True)   # direct → not governed
    assert "http://direct:9000/mcp" not in toolmod._GOVERNED_URLS


def test_load_contract_rejects_cyclic_graph_at_reconcile(tmp_path):
    # consultant #2: reconcile-time (pod load) validation rejects a cyclic declared graph, not only codegen
    import yaml
    from agentgate.server import _load_contract
    p = tmp_path / "org.yaml"
    p.write_text(yaml.safe_dump({"spec": {
        "agents": [{"name": "a"}, {"name": "b"}],
        "delegations": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }}))
    with pytest.raises(ValueError, match="cycle"):
        _load_contract(str(p))


def test_register_mcp_tools_strict_raises_on_unreachable(monkeypatch):
    # consultant: strict (prod readiness) — an unreachable backend RAISES instead of degrading to []
    import sys
    import types
    from agentgate.codegen import register_mcp_tools

    class _Boom:
        def __init__(self, url): pass
        async def __aenter__(self): raise TimeoutError("down")
        async def __aexit__(self, *a): return False

    mod = types.ModuleType("fastmcp")
    mod.Client = _Boom
    monkeypatch.setitem(sys.modules, "fastmcp", mod)
    with pytest.raises(RuntimeError, match="unreachable"):
        register_mcp_tools("k8s", "http://nope/mcp", strict=True)


def test_make_router_quarantines_undeclared_next():
    # consultant #5: even in log mode an undeclared next must route to END, never into the graph
    from agentgate.codegen import make_router
    r = make_router(action="log", allowed=["coder"])
    assert r({"next": "coder"}) == "coder"          # declared target → route
    assert r({"next": "reviewer"}) == "__end__"     # undeclared pick → quarantine to END
    assert r({"next": None}) == "__end__"           # dropped → END


# --- multi-provider LLM: openai-compatible (OpenAI / Azure / RunPod / vLLM) ---

def test_maybe_llm_openai_compatible_runpod(monkeypatch):
    # provider=openai (or runpod/azure/vllm) → /v1/chat/completions with a bearer key; RunPod is just
    # an endpoint (base_url). No plaintext key in config — it comes from <PROVIDER>_API_KEY.
    from agentgate.codegen.runtime import _maybe_llm
    captured: dict = {}

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "done", "tool_calls": []}}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return _R()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("RUNPOD_API_KEY", "rp-secret")
    out = _maybe_llm(name="ops", model="llama-3.1-70b", instructions="i", state={"goal": "g"},
                     tools=(), provider="runpod",
                     endpoint="https://api.runpod.ai/v2/abc123/openai/v1")
    assert out == "done"
    assert captured["url"] == "https://api.runpod.ai/v2/abc123/openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer rp-secret"


def test_maybe_llm_unknown_provider_lists_supported(monkeypatch):
    from agentgate.codegen.runtime import _maybe_llm
    monkeypatch.delenv("AGENTGATE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DRIFTWATCH_LLM_PROVIDER", raising=False)
    with pytest.raises(ValueError, match="openai-compatible"):
        _maybe_llm(name="a", model="m", instructions="i", state={"goal": "g"}, tools=(),
                   provider="cohere")


def test_maybe_llm_anthropic(monkeypatch):
    # anthropic Messages API (/v1/messages, x-api-key); tool format differs but basic chat returns text
    from agentgate.codegen.runtime import _maybe_llm
    captured = {}

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": "done"}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return _R()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    out = _maybe_llm(name="a", model="claude-3-5", instructions="i", state={"goal": "g"},
                     tools=(), provider="anthropic")
    assert out == "done"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant"


def test_maybe_llm_gemini(monkeypatch):
    # gemini generateContent (?key=...); functionDeclarations format, basic chat returns parts[].text
    from agentgate.codegen.runtime import _maybe_llm
    captured = {}

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"candidates": [{"content": {"parts": [{"text": "done"}]}}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        return _R()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    out = _maybe_llm(name="a", model="gemini-2.0", instructions="i", state={"goal": "g"},
                     tools=(), provider="gemini")
    assert out == "done"
    assert "generateContent?key=g-key" in captured["url"]


def test_maybe_llm_bedrock(monkeypatch):
    # bedrock = AWS SDK (boto3) Converse API — not openai-compatible; mock the client
    import sys
    import types
    fake = types.ModuleType("boto3")

    class _Client:
        def converse(self, **kw):
            return {"output": {"message": {"content": [{"text": "done"}]}}}

    fake.client = lambda *a, **k: _Client()
    monkeypatch.setitem(sys.modules, "boto3", fake)
    from agentgate.codegen.runtime import _maybe_llm
    out = _maybe_llm(name="a", model="anthropic.claude-3", instructions="i", state={"goal": "g"},
                     tools=(), provider="bedrock")
    assert out == "done"
