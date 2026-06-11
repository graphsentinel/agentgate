"""AgentGate HTTP server — serve a declared AgenticArchitecture, run it via POST /run."""
from __future__ import annotations

import asyncio

import pytest

from agentgate.library.contract import build_contract

SPEC = {
    "agents": [
        {"name": "planner", "tier": "strategic", "instructions": "plan"},
        {"name": "coder", "instructions": "code"},
        {"name": "reviewer", "instructions": "review"},
    ],
    "delegations": [{"from": "planner", "to": "coder"}, {"from": "coder", "to": "reviewer"}],
}


def _call(method: str, path: str, **kw):
    pytest.importorskip("fastapi")
    pytest.importorskip("langgraph")
    from httpx import ASGITransport, AsyncClient

    from agentgate.server import build_app
    app = build_app(build_contract(SPEC))

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.request(method, path, **kw)

    return asyncio.run(_go())


def test_healthz():
    assert _call("GET", "/healthz").json() == {"status": "ok"}


def test_info_lists_agents_and_coordinator():
    info = _call("GET", "/").json()
    assert info["service"] == "agentgate"
    assert info["coordinator"] == "planner"
    assert info["agents"] == ["coder", "planner", "reviewer"]


def test_run_drives_the_graph_from_the_coordinator():
    r = _call("POST", "/run", json={"goal": "do a thing"}).json()
    assert r["coordinator"] == "planner"
    ran = [h["agent"] for h in r["history"]]
    assert ran[0] == "planner" and "reviewer" in ran     # flowed down the declared graph
    assert r["violations"] == []                          # stub static run, no drift
