"""AgentGate CLI: `generate <spec.yaml>` and `run <spec.yaml> --goal ...`.

E13 orchestration-as-code: turn an AgenticArchitecture YAML into a runnable framework app
(`generate`), or generate + execute it commanded via the coordinator (`run`). The drift/governance
side (demo, eval, consensus-seed) lives in DriftWatch — AgentGate only declares/generates/executes.
"""
from __future__ import annotations

import argparse
import sys


def _load_spec(path: str) -> dict:
    """Load an AgenticArchitecture from YAML — a Kubernetes CR (use `.spec`) or a bare ASL doc."""
    import yaml
    with open(path) as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    from .library.contract import resolve_instructions
    return resolve_instructions(doc.get("spec", doc))   # CR → .spec; load instructionsFrom too


def generate_main(argv: list[str] | None = None) -> int:
    """`agentgate generate <spec.yaml>` — E13 codegen: YAML → a runnable framework app."""
    from .codegen import TARGETS, coordinator, generate
    from .library.contract import build_contract

    parser = argparse.ArgumentParser(prog="agentgate generate")
    parser.add_argument("spec", help="AgenticArchitecture YAML (a CR or a bare ASL doc)")
    parser.add_argument("--target", default="langgraph", choices=sorted(TARGETS),
                        help="codegen target framework (default: langgraph)")
    parser.add_argument("--dynamic", action="store_true",
                        help="E13 6b: runtime-gated dynamic graph (langgraph only) instead of static")
    parser.add_argument("-o", "--out", default=None, help="output .py file (default: stdout)")
    args = parser.parse_args(argv)

    contract = build_contract(_load_spec(args.spec))
    code = generate(contract, args.target, dynamic=args.dynamic)
    if args.out:
        with open(args.out, "w") as f:
            f.write(code)
        print(f"wrote {args.out}: {len(contract.agents)} agents, "
              f"entry={coordinator(contract)} ({args.target})", file=sys.stderr)
    else:
        print(code)
    return 0


def run_main(argv: list[str] | None = None) -> int:
    """`agentgate run <spec.yaml> --goal ...` — generate + execute, commanded via the coordinator."""
    from .codegen import coordinator, generate, register_mcp_tools
    from .library.contract import build_contract

    parser = argparse.ArgumentParser(prog="agentgate run")
    parser.add_argument("spec", help="AgenticArchitecture YAML")
    parser.add_argument("--goal", required=True, help="the goal handed to the coordinator")
    parser.add_argument("--dynamic", action="store_true",
                        help="E13 6b: runtime-gated dynamic delegation (agents pick the next hand-off)")
    args = parser.parse_args(argv)

    contract = build_contract(_load_spec(args.spec))
    for srv_name, srv_url, srv_ns, srv_gv in contract.mcp_servers:   # import external MCP tools
        register_mcp_tools(srv_name, srv_url, namespace=srv_ns, governed=srv_gv)
    ns: dict = {}
    exec(compile(generate(contract, "langgraph", dynamic=args.dynamic), "<generated>", "exec"), ns)  # noqa: S102
    graph = ns["build_graph"]()

    import os
    print(f"== goal: {args.goal}", file=sys.stderr)
    provider = os.environ.get("AGENTGATE_LLM_PROVIDER") or os.environ.get("DRIFTWATCH_LLM_PROVIDER") or "stub"
    print(f"== coordinator: {coordinator(contract)} (LLM: {provider}"
          f"{', dynamic' if args.dynamic else ''})", file=sys.stderr)
    result = graph.invoke({"goal": args.goal, "history": []})
    for h in result.get("history", []):
        print(f"\n────── {h['agent']} ({h['model'] or 'stub'}) ──────")
        for call in h.get("tool_calls", []):   # observability: tools the agent used / was refused
            print(f"   · tool {call['tool']}: {'ok' if call['ok'] else 'REFUSED (unbound)'}")
        if h.get("delegation_violation"):
            dv = h["delegation_violation"]
            print(f"   ⚠ delegation BLOCKED → {dv['dst']}: {dv['reason']}")
        print((h.get("output") or "(stub — set AGENTGATE_LLM_PROVIDER=ollama for live output)").strip())
    if result.get("violations"):
        print(f"\n== delegation violations: {len(result['violations'])}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentgate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    from .codegen import TARGETS
    g = sub.add_parser("generate", help="E13: generate a framework app from an AgenticArchitecture")
    g.add_argument("spec")
    g.add_argument("--target", default="langgraph", choices=sorted(TARGETS))
    g.add_argument("--dynamic", action="store_true")
    g.add_argument("-o", "--out", default=None)
    r = sub.add_parser("run", help="E13: generate + run an AgenticArchitecture, via the coordinator")
    r.add_argument("spec")
    r.add_argument("--goal", required=True)
    r.add_argument("--dynamic", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "generate":
        argv_g = [args.spec, "--target", args.target] + (["-o", args.out] if args.out else [])
        if args.dynamic:
            argv_g.append("--dynamic")
        return generate_main(argv_g)
    if args.cmd == "run":
        argv_r = [args.spec, "--goal", args.goal] + (["--dynamic"] if args.dynamic else [])
        return run_main(argv_r)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
