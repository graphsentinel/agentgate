"""E13 execute layer — the runtime a generated LangGraph app calls into (design §A, phase 4).

The generated module references `make_agent_node` for each agent, so the generated code stays small
and the agent body is swappable here without regenerating. MVP ships a **deterministic, LLM-free**
node so the generated graph runs in CI/tests with no model or API key; a real LLM (a LangChain chat
model selected by `model`, bound to `tools`, system-prompted with `instructions`) plugs into the
same seam without touching generated code.
"""
from __future__ import annotations

import os
from collections.abc import Callable


def _env(key: str, default: str = "") -> str:
    """Read AGENTGATE_<key>, falling back to the legacy DRIFTWATCH_<key>, then `default`."""
    return os.environ.get(f"AGENTGATE_{key}") or os.environ.get(f"DRIFTWATCH_{key}") or default


_EMITTER = None
_EMITTER_READY = False


def _emitter():
    """Lazily build a gen_ai.agent.* OTLP emitter from AGENTGATE_OTLP_ENDPOINT (None → no-op).

    Module-level singleton so a generated graph emits without any change to generated code: set the
    endpoint (e.g. localhost:4317, the OTel Collector) and every agent run / tool call / delegation
    gate becomes a span in Jaeger + a metric in Prometheus/Grafana. Unset → no emitter, no overhead.
    """
    global _EMITTER, _EMITTER_READY
    if not _EMITTER_READY:
        _EMITTER_READY = True
        endpoint = _env("OTLP_ENDPOINT")
        if endpoint:
            from ..otel.emit import Emitter
            _EMITTER = Emitter(service_name="agentgate", endpoint=endpoint)
    return _EMITTER


def make_agent_node(
    *, name: str, model: str = "", instructions: str = "", tools: tuple[str, ...] | list[str] = (),
    can_delegate_to: tuple[str, ...] | list[str] = (), contract=None, delegation_action: str = "block",
    emit_attributes: tuple[str, ...] | list[str] = (),
    llm_provider: str = "", llm_endpoint: str = "",
    mcp_backends: tuple[str, ...] | list[str] = (),
) -> Callable[[dict], dict]:
    """Return a LangGraph node callable for one declared agent.

    The node takes the run state (a dict) and returns it with this agent's contribution appended to
    `state["history"]` and `state["last"]` set — proving the declared graph executed.

    LLM is **opt-in via env** so CI stays model-free: with no `AGENTGATE_LLM_PROVIDER` (default) the
    node is a deterministic stub; set `AGENTGATE_LLM_PROVIDER=ollama` (+ optional
    `AGENTGATE_OLLAMA_HOST`) to make agents think. (Legacy `DRIFTWATCH_*` names still work.)

    **Dynamic delegation (E13 6b):** when `can_delegate_to` is set, the node CHOOSES the next agent at
    run time (parsed from the model's `NEXT: <agent>` line, else the first declared target) and GATES
    it with `contract.check_delegation` against the declared graph + active path. An allowed pick is
    written to `state["next"]`; a violation is recorded on `state["violations"]` and, if
    `delegation_action == "block"`, the hand-off is dropped (`next=None` → the router goes to END).
    Static graphs leave `can_delegate_to` empty and never reach this path.
    """
    tools = tuple(tools)
    can_delegate_to = tuple(can_delegate_to)
    mcp_backends = tuple(mcp_backends)

    def _node(state: dict) -> dict:
        state = dict(state)
        history = list(state.get("history", []))
        # bound = explicit tools + every tool from the agent's whole-backend bindings (resolved at
        # run time, after register_mcp_tools has populated the backends)
        from .tools import McpSession, backend_tools_filtered
        eff_tools = tuple(tools) + tuple(
            t for t in backend_tools_filtered(mcp_backends) if t not in tools)
        entry: dict = {"agent": name, "model": model, "tools": list(eff_tools)}
        tool_trace: list[dict] = []   # observability: which tools were called / refused this turn
        # dynamic mode: ask the model to choose the next agent (the gate then checks the pick)
        eff_instructions = instructions
        if can_delegate_to:
            eff_instructions = (instructions + f"\n\nThen choose the next agent from "
                                f"{list(can_delegate_to)} and end your reply with: NEXT: <agent>.")
        # one MCP session for this agent-run so the proxy groups the tool calls into one chain;
        # meta carries the prompt/agent/task_type (agent-side of the §4c prompt-aware cross-check)
        goal = str(state.get("goal", ""))
        # candidates (tools) let DriftWatch's cross-check predict from the agent's actual tool set
        run_meta = {"agent": name, "task_type": goal[:80], "prompt": goal, "tools": list(eff_tools)}
        with McpSession(meta=run_meta):
            output = _maybe_llm(name=name, model=model, instructions=eff_instructions, state=state,
                                tools=eff_tools, tool_trace=tool_trace,
                                provider=llm_provider, endpoint=llm_endpoint)
        if output is not None:
            entry["output"] = output
        if tool_trace:
            entry["tool_calls"] = tool_trace

        if can_delegate_to:   # dynamic: pick + gate the next hand-off
            nxt = _choose_next(can_delegate_to, output)
            path = tuple(h.get("agent", "") for h in history)
            reason = contract.check_delegation(name, nxt, active_path=path) if (contract and nxt) else None
            if reason:
                entry["delegation_violation"] = {"dst": nxt, "reason": reason}
                state["violations"] = [*state.get("violations", []),
                                       {"src": name, "dst": nxt, "reason": reason}]
                if delegation_action == "block":
                    nxt = None   # drop the undeclared hand-off → router routes to END
            state["next"] = nxt

        em = _emitter()   # gen_ai.agent.* span per run (no-op unless an OTLP endpoint is set)
        if em is not None:
            em.emit_agent_run(agent_id=name, task_type=str(state.get("goal", ""))[:80],
                              model=model, tools=list(eff_tools), tool_calls=tool_trace,
                              violation=entry.get("delegation_violation"),
                              attributes=emit_attributes)

        history.append(entry)
        state["history"] = history
        state["last"] = name
        return state

    _node.__name__ = f"agent_{name}"
    return _node


def _choose_next(candidates: tuple[str, ...], output: str | None) -> str | None:
    """Pick the next agent: the model's `NEXT: <agent>` line if valid, else the first declared target.

    The stub (no LLM) and a model that doesn't emit a NEXT line both fall back to the first declared
    candidate — deterministic. A model MAY pick any name; if it picks one outside `candidates` we
    keep that pick so the gate (check_delegation) can flag it as a novel edge rather than silently
    correcting it.
    """
    if output:
        for line in reversed(output.splitlines()):
            if line.strip().upper().startswith("NEXT:"):
                return line.split(":", 1)[1].strip() or (candidates[0] if candidates else None)
    return candidates[0] if candidates else None


def make_router(*, action: str = "block",
                allowed: tuple[str, ...] | list[str] = ()) -> Callable[[dict], str]:
    """A LangGraph conditional-edge function: route to `state['next']`, or END.

    The gating happened in the node (make_agent_node), which set `state['next']` (a pick) or None
    (blocked). This router turns that into a routing decision and, critically (consultant #5),
    **quarantines** any `next` that is not a declared target to `__end__` — so even in `log` mode an
    undeclared hand-off cannot route into the graph (the conditional mapping only has declared
    targets + END; an unmapped key would otherwise blow up). `allowed` is the declared target set.
    """
    allow = set(allowed)
    def _route(state: dict) -> str:
        nxt = state.get("next")
        if nxt and (not allow or nxt in allow):
            return nxt
        return "__end__"   # dropped (None), or an undeclared pick → quarantine to END
    return _route


def _maybe_llm(
    *, name: str, model: str, instructions: str, state: dict, tools: tuple[str, ...],
    tool_trace: list[dict] | None = None, provider: str = "", endpoint: str = "",
) -> str | None:
    """Call the configured LLM, or None when no live provider is set (CI/test default → stub).

    Provider/endpoint resolution: per-agent/global value (passed in) ?? env (the floor).
    """
    provider = (provider or _env("LLM_PROVIDER")).lower()
    if not provider or not model:
        return None
    if provider == "ollama":
        return _ollama_chat(model=model, instructions=instructions, state=state, name=name,
                            tool_names=tools, tool_trace=tool_trace, endpoint=endpoint)
    # OpenAI-compatible: openai, azure, runpod, vllm, tgi, or any serverless /v1 endpoint. The
    # `endpoint` is the base_url (e.g. https://api.runpod.ai/v2/<id>/openai/v1); the key comes from
    # <PROVIDER>_API_KEY (falling back to OPENAI_API_KEY). No plaintext key in the CRD.
    if provider in ("openai", "azure", "runpod", "vllm", "tgi", "openai-compatible"):
        # API keys use their standard names (OPENAI_API_KEY, RUNPOD_API_KEY, …) — NOT the AGENTGATE_/
        # DRIFTWATCH_ prefix; read os.environ directly (key never lives in the CRD).
        key = (os.environ.get(f"{provider.upper().replace('-', '_')}_API_KEY")
               or os.environ.get("OPENAI_API_KEY", ""))
        return _openai_chat(model=model, instructions=instructions, state=state, name=name,
                            tool_names=tools, tool_trace=tool_trace, endpoint=endpoint, api_key=key)
    if provider == "anthropic":
        return _anthropic_chat(model=model, instructions=instructions, state=state, name=name,
                               tool_names=tools, tool_trace=tool_trace, endpoint=endpoint,
                               api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    raise ValueError(
        f"unknown LLM provider {provider!r} "
        f"(supported: ollama, openai-compatible[openai/azure/runpod/vllm/tgi], anthropic; "
        f"gemini/bedrock roadmap)")


def _ollama_chat(
    *, model: str, instructions: str, state: dict, name: str, tool_names: tuple[str, ...] = (),
    tool_trace: list[dict] | None = None, endpoint: str = "",
) -> str:
    """A chat turn (with a tool loop) against Ollama's /api/chat.

    **Creation-driven binding:** the model is offered ONLY this agent's bound tools, so it cannot
    call a tool it was not granted. When the model requests a tool, we run the registered callable
    and feed the result back, looping until the model answers (bounded by AGENTGATE_TOOL_ITERS).
    """
    import httpx

    from .tools import bound_tools, get_tool

    host = (endpoint or _env("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
    timeout = float(_env("LLM_TIMEOUT", "180"))
    max_iters = int(_env("TOOL_ITERS", "4"))
    allowed = set(tool_names)
    schema = [t.as_ollama_schema() for t in bound_tools(tool_names)]   # bound tools only

    goal = state.get("goal", "")
    prior = "\n".join(
        f"- {h['agent']}: {h['output']}" for h in state.get("history", []) if h.get("output")
    )
    user = f"Goal: {goal}\n\nWork so far:\n{prior}" if prior else f"Goal: {goal}"
    messages: list[dict] = [
        {"role": "system", "content": instructions or f"You are the {name} agent."},
        {"role": "user", "content": user},
    ]

    last_content = ""
    for _ in range(max_iters):
        payload: dict = {"model": model, "messages": messages, "stream": False}
        if schema:
            payload["tools"] = schema
        resp = httpx.post(f"{host}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
        msg = resp.json().get("message", {}) or {}
        last_content = msg.get("content", "") or last_content
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content", "")
        messages.append(msg)   # assistant turn carrying the tool_calls
        for tc in calls:
            fn = (tc.get("function") or {})
            tname, targs = fn.get("name", ""), fn.get("arguments") or {}
            tool = get_tool(tname)
            bound = tname in allowed and tool is not None
            if not bound:
                # the model asked for an un-bound tool — refuse (defence in depth; schema already
                # excludes it). This is the creation-driven binding holding at run time.
                result = f"error: tool {tname!r} is not bound to agent {name!r}"
            else:
                try:
                    result = tool.func(**targs) if isinstance(targs, dict) else tool.func(targs)
                except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                    result = f"error: {e}"
            if tool_trace is not None:   # observability: record the call + whether it was allowed
                tool_trace.append({"tool": tname, "ok": bound})
            messages.append({"role": "tool", "tool_name": tname, "content": str(result)})
    return last_content   # tool loop exhausted; return the model's last words


def _openai_chat(
    *, model: str, instructions: str, state: dict, name: str, tool_names: tuple[str, ...] = (),
    tool_trace: list[dict] | None = None, endpoint: str = "", api_key: str = "",
) -> str:
    """A chat turn (with a tool loop) against an OpenAI-compatible /v1/chat/completions endpoint.

    Covers OpenAI, Azure OpenAI, RunPod serverless (vLLM/TGI), and any compatible server — `endpoint`
    is the base_url, `api_key` the bearer token. Same creation-driven binding as Ollama: the model is
    offered only its bound tools; an unbound call is refused.
    """
    import json

    import httpx

    from .tools import bound_tools, get_tool

    base = (endpoint or _env("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
    timeout = float(_env("LLM_TIMEOUT", "180"))
    max_iters = int(_env("TOOL_ITERS", "4"))
    allowed = set(tool_names)
    schema = [t.as_ollama_schema() for t in bound_tools(tool_names)]   # OpenAI tool schema (same shape)

    goal = state.get("goal", "")
    prior = "\n".join(
        f"- {h['agent']}: {h['output']}" for h in state.get("history", []) if h.get("output")
    )
    user = f"Goal: {goal}\n\nWork so far:\n{prior}" if prior else f"Goal: {goal}"
    messages: list[dict] = [
        {"role": "system", "content": instructions or f"You are the {name} agent."},
        {"role": "user", "content": user},
    ]
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    last_content = ""
    for _ in range(max_iters):
        payload: dict = {"model": model, "messages": messages, "stream": False}
        if schema:
            payload["tools"] = schema
        resp = httpx.post(f"{base}/chat/completions", json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        msg = ((resp.json().get("choices") or [{}])[0] or {}).get("message", {}) or {}
        last_content = msg.get("content") or last_content
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content", "") or ""
        messages.append(msg)   # assistant turn carrying the tool_calls
        for tc in calls:
            fn = tc.get("function") or {}
            tname = fn.get("name", "")
            raw_args = fn.get("arguments")
            try:
                targs = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:  # noqa: BLE001 — malformed args → empty, tool will error cleanly
                targs = {}
            tool = get_tool(tname)
            bound = tname in allowed and tool is not None
            if not bound:
                result = f"error: tool {tname!r} is not bound to agent {name!r}"
            else:
                try:
                    result = tool.func(**targs) if isinstance(targs, dict) else tool.func(targs)
                except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                    result = f"error: {e}"
            if tool_trace is not None:
                tool_trace.append({"tool": tname, "ok": bound})
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": str(result)})
    return last_content


def _anthropic_chat(
    *, model: str, instructions: str, state: dict, name: str, tool_names: tuple[str, ...] = (),
    tool_trace: list[dict] | None = None, endpoint: str = "", api_key: str = "",
) -> str:
    """A chat turn (with a tool loop) against Anthropic's Messages API (/v1/messages).

    Anthropic's tool format differs from OpenAI's: tools are {name, description, input_schema};
    tool calls arrive as `tool_use` content blocks; results go back as `tool_result` blocks. Same
    creation-driven binding (only bound tools offered; unbound refused).
    """
    import httpx

    from .tools import bound_tools, get_tool

    base = (endpoint or "https://api.anthropic.com").rstrip("/")
    timeout = float(_env("LLM_TIMEOUT", "180"))
    max_iters = int(_env("TOOL_ITERS", "4"))
    max_tokens = int(_env("MAX_TOKENS", "1024"))
    allowed = set(tool_names)
    tools_schema = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                    for t in bound_tools(tool_names)]

    goal = state.get("goal", "")
    prior = "\n".join(
        f"- {h['agent']}: {h['output']}" for h in state.get("history", []) if h.get("output")
    )
    user = f"Goal: {goal}\n\nWork so far:\n{prior}" if prior else f"Goal: {goal}"
    messages: list[dict] = [{"role": "user", "content": user}]
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}

    last_text = ""
    for _ in range(max_iters):
        payload: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if instructions:
            payload["system"] = instructions
        if tools_schema:
            payload["tools"] = tools_schema
        resp = httpx.post(f"{base}/v1/messages", json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content = resp.json().get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        last_text = text or last_text
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            return text
        messages.append({"role": "assistant", "content": content})
        results: list[dict] = []
        for tu in tool_uses:
            tname = tu.get("name", "")
            targs = tu.get("input") or {}
            tool = get_tool(tname)
            bound = tname in allowed and tool is not None
            if not bound:
                result = f"error: tool {tname!r} is not bound to agent {name!r}"
            else:
                try:
                    result = tool.func(**targs) if isinstance(targs, dict) else tool.func(targs)
                except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                    result = f"error: {e}"
            if tool_trace is not None:
                tool_trace.append({"tool": tname, "ok": bound})
            results.append({"type": "tool_result", "tool_use_id": tu.get("id", ""),
                            "content": str(result)})
        messages.append({"role": "user", "content": results})
    return last_text
