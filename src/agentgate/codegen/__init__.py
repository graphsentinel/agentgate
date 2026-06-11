"""E13 codegen — turn a declared `AgenticArchitecture` into a runnable multi-agent app.

One YAML → one framework app, single pod (Docs/e13-mabac-delegation-design.md §B). The first target
is LangGraph. **Creation-driven governance:** only declared delegation edges are emitted, so a
forbidden (undeclared) hand-off cannot exist in the generated graph — the rule is enforced by
absence, not a runtime gate.
"""
from __future__ import annotations

from ..library.contract import DeclaredContract
from .autogen import generate_autogen
from .crewai import generate_crewai
from .langgraph import coordinator, generate_langgraph
from .runtime import make_agent_node, make_router
from .tools import (
    McpSession,
    Tool,
    backend_tools,
    backend_tools_filtered,
    bound_tools,
    get_tool,
    register_mcp_tools,
    register_tool,
)
from .validate import find_cycle, topological_order, validate_for_generation

# target name -> generator. The same contract generates any of these (framework-agnostic).
TARGETS = {
    "langgraph": generate_langgraph,
    "autogen": generate_autogen,
    "crewai": generate_crewai,
}


def generate(contract: DeclaredContract, target: str = "langgraph", *, dynamic: bool = False) -> str:
    """Generate app source for `target` from a declared contract (one YAML, any framework).

    `dynamic=True` (E13 6b, LangGraph only) emits a runtime-gated dynamic graph; otherwise static
    (creation-driven). Dynamic is currently LangGraph-only.
    """
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r} (choices: {sorted(TARGETS)})")
    if dynamic:
        if target != "langgraph":
            raise ValueError(f"dynamic generation is langgraph-only (not {target!r})")
        return generate_langgraph(contract, dynamic=True)
    return TARGETS[target](contract)


__all__ = [
    "generate",
    "TARGETS",
    "generate_langgraph",
    "generate_autogen",
    "generate_crewai",
    "coordinator",
    "validate_for_generation",
    "topological_order",
    "find_cycle",
    "make_agent_node",
    "make_router",
    "Tool",
    "register_tool",
    "register_mcp_tools",
    "get_tool",
    "bound_tools",
    "backend_tools",
    "backend_tools_filtered",
    "McpSession",
]
