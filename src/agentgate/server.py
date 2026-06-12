"""AgentGate HTTP server — generate the declared app once, run it per request.

At startup the AgenticArchitecture (a mounted YAML or a CR's `.spec`) is built into a contract and
generated into a LangGraph app (static or `AGENTGATE_DYNAMIC`); the compiled graph is reused for
every request. `POST /run {"goal": ...}` invokes it through the coordinator and returns the run trace.
FastAPI/uvicorn (`.[interceptor]`) and langgraph (`.[codegen]`) are required at runtime.
"""
from __future__ import annotations

import os

from .library.contract import DeclaredContract, build_contract, resolve_instructions


def _maybe_govern(contract: DeclaredContract, register_mcp_tools, *, strict: bool = False) -> None:
    """E13 §4e — single-source interop. If `spec.govern.proxyType == driftwatch`:

    1. **push the contract once** to `govern.register` (DriftWatch stores it as the declared contract,
       source=agentgate) — so the org is declared only here, never re-applied to DriftWatch;
    2. **route the tool path** — register `govern.endpoint` (the DriftWatch MCP proxy) as a governed
       backend, so agents' tool calls flow through governance.

    `proxyType` empty/none → no-op (standalone). Push failures degrade gracefully unless `strict`.
    """
    gov = contract.govern or {}
    if (gov.get("proxyType") or "none").lower() != "driftwatch":
        return
    register = gov.get("register")
    if register:
        try:
            import httpx
            httpx.post(register, json={"source": "agentgate", "contract": contract.to_dict()},
                       timeout=15.0).raise_for_status()
        except Exception as e:  # noqa: BLE001 — DriftWatch unreachable
            if strict:
                raise
            # proxyType=driftwatch means the user EXPECTS governance — make a failed push loud, not
            # silent, even when degrading (consultant): the tool path still routes to the proxy.
            import logging
            logging.getLogger("agentgate.govern").warning(
                "contract push to DriftWatch (%s) failed: %s — agents will still route to the proxy, "
                "but DriftWatch has no declared contract until the next successful push", register, e)
    endpoint = gov.get("endpoint")
    if endpoint:   # governed tool path: the DriftWatch proxy (already namespaced) as a governed backend
        register_mcp_tools("driftwatch", endpoint, namespace=False, governed=True, strict=strict)


def _compile_graph(contract: DeclaredContract, *, dynamic: bool):
    """Generate the LangGraph app for `contract` and return the compiled graph."""
    from .codegen import generate
    ns: dict = {}
    exec(compile(generate(contract, "langgraph", dynamic=dynamic),  # noqa: S102 — our generated app
                 "<agentgate>", "exec"), ns)
    return ns["build_graph"]()


def build_app(contract: DeclaredContract, *, dynamic: bool = False):  # pragma: no cover - needs deps
    """A FastAPI app serving the generated multi-agent graph for `contract`."""
    from fastapi import Body, FastAPI

    import os

    from .codegen import coordinator, register_mcp_tools

    # strict mode (prod readiness): an unreachable backend fails startup → pod won't go Ready, rather
    # than silently serving a tool-less agent. Off by default (dev/standalone degrade gracefully).
    strict = (os.environ.get("AGENTGATE_MCP_STRICT") or os.environ.get("DRIFTWATCH_MCP_STRICT")
              or "").strip().lower() == "true"
    for srv_name, srv_url, srv_ns, srv_gv in contract.mcp_servers:   # import external MCP tools
        register_mcp_tools(srv_name, srv_url, namespace=srv_ns, governed=srv_gv, strict=strict)
    # E13 §4e — govern.proxyType=driftwatch: push the contract once + route tool calls via the proxy.
    _maybe_govern(contract, register_mcp_tools, strict=strict)
    graph = _compile_graph(contract, dynamic=dynamic)
    entry = coordinator(contract)
    app = FastAPI(title="agentgate")

    @app.get("/")
    def info():
        return {"service": "agentgate", "agents": sorted(contract.agents),
                "coordinator": entry, "dynamic": dynamic}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/run")
    def run_goal(payload: dict = Body(...)):
        result = graph.invoke({"goal": payload.get("goal", ""), "history": []})
        return {
            "coordinator": entry,
            "history": result.get("history", []),
            "violations": result.get("violations", []),
        }

    return app


def _load_contract(spec_path: str) -> DeclaredContract:
    """Build + VALIDATE the contract from a mounted AgenticArchitecture YAML (CR `.spec` or ASL doc).

    Reconcile-time validation (consultant review): the declared graph is checked DAG + scope-monotonic
    here, not only at codegen — a cyclic / scope-escalating org fails to load (the pod won't go Ready)
    rather than silently running a malformed graph.
    """
    import yaml

    from .codegen.validate import validate_for_generation
    with open(spec_path) as f:
        doc = yaml.safe_load(f)
    spec = doc.get("spec", doc) if isinstance(doc, dict) else doc
    contract = build_contract(resolve_instructions(spec))   # load instructionsFrom (configMap/path)
    validate_for_generation(contract)                       # DAG + scope monotonic; raises on violation
    return contract


def run() -> None:  # pragma: no cover - console entry point
    """Entry point: read the mounted AgenticArchitecture, serve it on :8000."""
    import uvicorn

    spec_path = os.environ.get("AGENTGATE_SPEC_PATH", "/etc/agentgate/org.yaml")
    dynamic = os.environ.get("AGENTGATE_DYNAMIC", "").lower() in ("1", "true", "yes")
    contract = _load_contract(spec_path)
    uvicorn.run(build_app(contract, dynamic=dynamic), host="0.0.0.0", port=8000)
