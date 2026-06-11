"""E13 live demo — one YAML → a running LangGraph multi-agent app, driven by a real LLM (Ollama).

    # stub (no LLM, proves the graph wiring):
    PYTHONPATH=../../src python demo.py "reverse a string"

    # live (real agents via Ollama):
    AGENTGATE_LLM_PROVIDER=ollama PYTHONPATH=../../src python demo.py "reverse a string"

Needs the `codegen` extra (langgraph) installed; live mode needs Ollama at $AGENTGATE_OLLAMA_HOST
(default http://localhost:11434) serving MODEL below. Shows the whole E13 MVP: declare → generate →
execute, commanded through the coordinator, flowing down the declared delegation graph.
"""
from __future__ import annotations

import sys

from driftwatch.codegen import coordinator, generate_langgraph
from driftwatch.library.contract import build_contract

MODEL = "qwen3.5:397b-cloud"

# The whole org as code (one YAML-equivalent dict): 3 agents, a delegation DAG, instructions/model.
SPEC = {
    "agents": [
        {"name": "planner", "tier": "strategic", "model": MODEL,
         "instructions": "You are a planner. Break the goal into 3 short numbered steps. "
                         "Output only the numbered steps, nothing else."},
        {"name": "coder", "model": MODEL,
         "instructions": "You are a coder. Given the plan, implement step 1 as a short Python "
                         "function. Output only the code block."},
        {"name": "reviewer", "model": MODEL,
         "instructions": "You are a reviewer. Point out exactly one concrete issue or improvement "
                         "in the code. Output 1-2 sentences."},
    ],
    "delegations": [
        {"from": "planner", "to": "coder"},
        {"from": "coder", "to": "reviewer"},
    ],
}


def main() -> None:
    goal = sys.argv[1] if len(sys.argv) > 1 else "Write a function that reverses a string."
    contract = build_contract(SPEC)
    source = generate_langgraph(contract)           # declare → generate (creation-driven)

    ns: dict = {}
    exec(compile(source, "<e13-demo>", "exec"), ns)  # noqa: S102 — running our generated app
    graph = ns["build_graph"]()                       # execute: compile the LangGraph

    print(f"== goal       : {goal}")
    print(f"== coordinator: {coordinator(contract)}  (you command this one)")
    print(f"== graph      : {' -> '.join(a for a in sorted(contract.agents))} per declared edges\n")

    result = graph.invoke({"goal": goal, "history": []})   # command the coordinator
    for h in result["history"]:
        print(f"────── {h['agent']}  ({h['model'] or 'stub'}) ──────")
        print((h.get("output") or "(stub — set AGENTGATE_LLM_PROVIDER=ollama for live output)").strip())
        print()


if __name__ == "__main__":
    main()
