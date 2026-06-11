"""E13 tool registry — bind declared tool names to real callables (execute layer).

An agent's `tools` are declared in the YAML (E11 binding). At run time each name resolves to a
registered `Tool` (callable + JSON-schema). **Creation-driven binding:** the LLM is only ever
offered the agent's *bound* tools, so it cannot call a tool it was not granted — the E11 binding
enforced at the prompt boundary, not just by a post-hoc check.

Register your own tools with `@register_tool(...)`; a couple of safe demo tools ship here so the
example runs out of the box.
"""
from __future__ import annotations

import ast
import contextvars
import operator
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict              # JSON schema of the arguments object
    func: Callable[..., str]

    def as_ollama_schema(self) -> dict:
        """The OpenAI/Ollama function-tool schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_REGISTRY: dict[str, Tool] = {}
_BACKEND_TOOLS: dict[str, list[str]] = {}   # server name -> tool names it contributed (whole-backend)
_GOVERNED_URLS: set[str] = set()            # urls behind the DriftWatch proxy (namespace=False) — the
                                            # ONLY urls cross-check `_meta` (the prompt) is sent to


def backend_tools(backends: tuple[str, ...] | list[str]) -> list[str]:
    """All tool names contributed by the given MCP backends (by name, whole-backend, no filtering)."""
    out: list[str] = []
    for b in backends:
        out.extend(_BACKEND_TOOLS.get(b, []))
    return out


def backend_tools_filtered(
    backends: "tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] | list",
) -> list[str]:
    """Tools from each (name, allow, deny) backend, glob-filtered (least-privilege allowlist).

    `allow`/`deny` are `fnmatch` globs over the registered (namespaced) tool names. Empty allow = all;
    deny then removes. So `{name: k8s, allow: ['pods_*']}` offers only pods_* tools; a bare name
    (allow=deny=()) offers everything (default). Excluded tools are never registered for the agent →
    the LLM is never offered them (creation-driven).
    """
    import fnmatch
    out: list[str] = []
    for name, allow, deny in backends:
        ts = _BACKEND_TOOLS.get(name, [])
        if allow:
            ts = [t for t in ts if any(fnmatch.fnmatch(t, p) for p in allow)]
        if deny:
            ts = [t for t in ts if not any(fnmatch.fnmatch(t, p) for p in deny)]
        out.extend(ts)
    return out


def register_tool(name: str, description: str, parameters: dict) -> Callable[[Callable], Callable]:
    """Decorator: register a callable as a bindable tool under `name`."""
    def deco(fn: Callable[..., str]) -> Callable[..., str]:
        _REGISTRY[name] = Tool(name=name, description=description, parameters=parameters, func=fn)
        return fn
    return deco


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def bound_tools(names: tuple[str, ...] | list[str]) -> list[Tool]:
    """Resolve an agent's declared tool names to registered tools (unknown names are skipped)."""
    return [_REGISTRY[n] for n in names if n in _REGISTRY]


# --- external MCP servers: import their tools at startup (E13 §External tools) ---

# The active per-agent-run MCP session, if any. DriftWatch correlates a chain by MCP transport
# session id, so reusing ONE session per run makes all of a run's calls land in one chain (sequence
# drift + baseline). Outside a session (None) each call opens its own session (per-call only).
_MCP_SESSION: contextvars.ContextVar = contextvars.ContextVar("mcp_session", default=None)


class McpSession:
    """One MCP transport session per agent-run, reused for every tool call → DriftWatch sees one
    chain (it keys interceptors on the session id). One event loop, one entered Client per url.

    `meta` (e.g. {agent, task_type, prompt}) rides every call as the MCP request `_meta` map — the
    agent-side of the prompt-aware cross-check (§4c). Harmless until DriftWatch reads it.
    """

    def __init__(self, meta: dict | None = None) -> None:
        self.meta = meta or None

    def __enter__(self) -> McpSession:
        import asyncio
        self._loop = asyncio.new_event_loop()
        self._clients: dict[str, object] = {}
        self._token = _MCP_SESSION.set(self)
        return self

    def __exit__(self, *exc: object) -> None:
        for c in self._clients.values():
            try:
                self._loop.run_until_complete(c.__aexit__(None, None, None))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — best-effort close
                pass
        self._loop.close()
        _MCP_SESSION.reset(self._token)

    def call(self, url: str, tool_name: str, kwargs: dict) -> str:
        from fastmcp import Client
        c = self._clients.get(url)
        if c is None:
            c = Client(url)
            self._loop.run_until_complete(c.__aenter__())   # open ONCE, keep for the run
            self._clients[url] = c
        # The cross-check context rides as an `_meta` key in arguments (FastMCP call_tool(meta=) is
        # client-local). Two guards (consultant review): (1) fail-fast if a real tool argument is
        # named `_meta` — never silently overwrite it; (2) only send the prompt to GOVERNED (proxy)
        # urls — a direct MCP server would receive the prompt as a real arg (leak) and not strip it.
        if "_meta" in kwargs:
            raise ValueError("'_meta' is a reserved argument key (cross-check metadata)")
        send_meta = bool(self.meta) and url in _GOVERNED_URLS
        args = {**kwargs, "_meta": self.meta} if send_meta else kwargs
        return str(self._loop.run_until_complete(c.call_tool(tool_name, args)))


def _mcp_proxy(url: str, tool_name: str) -> Callable[..., str]:
    """A sync callable that forwards a call to an MCP server's tool (over FastMCP). If an McpSession
    is active (per agent-run), reuse it so the proxy groups calls into one chain; else open per-call."""
    def _call(**kwargs: object) -> str:
        sess = _MCP_SESSION.get()
        if sess is not None:
            try:
                return sess.call(url, tool_name, kwargs)
            except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                return f"error: {e}"
        import asyncio

        from fastmcp import Client

        async def _go() -> str:
            async with Client(url) as c:
                return str(await c.call_tool(tool_name, kwargs))
        try:
            return asyncio.run(_go())
        except Exception as e:  # noqa: BLE001 — surface tool errors to the model
            return f"error: {e}"
    return _call


def register_mcp_tools(server_name: str, url: str, *, namespace: bool = True,
                       governed: bool | None = None, strict: bool = False,
                       timeout: float = 15.0) -> list[str]:
    """Connect to an MCP server, list its tools, register each (under `server_name` as a backend).

    `namespace=True` registers tools as `<server_name>_<tool>`; `namespace=False` keeps the server's
    own tool names verbatim (passthrough). Returns the registered names. Best-effort with a `timeout`:
    an unreachable/slow server registers nothing and returns [] — standalone-safe. With `strict=True`
    (consultant: prod readiness) an unreachable backend instead RAISES, so the pod fails readiness
    rather than silently running a tool-less agent. `url` may be the server or the DriftWatch proxy.
    """
    import asyncio

    async def _list() -> list:
        from fastmcp import Client
        async with Client(url) as c:
            return await asyncio.wait_for(c.list_tools(), timeout=timeout)

    try:
        tools = asyncio.run(_list())
    except Exception as e:  # noqa: BLE001 — unreachable/slow server
        _BACKEND_TOOLS[server_name] = []
        if strict:
            raise RuntimeError(
                f"MCP backend {server_name!r} at {url} unreachable (strict mode)") from e
        return []   # degrade gracefully (dev/standalone)

    # governed=True ⟹ behind the proxy ⟹ safe to send prompt _meta. Explicit (consultant #4);
    # defaults to (not namespace) for back-compat when the caller doesn't say.
    if governed if governed is not None else (not namespace):
        _GOVERNED_URLS.add(url)
    registered: list[str] = []
    for t in tools:
        raw = getattr(t, "name", "")
        name = f"{server_name}_{raw}" if namespace else raw
        params = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        _REGISTRY[name] = Tool(name=name, description=getattr(t, "description", "") or "",
                               parameters=params, func=_mcp_proxy(url, raw))
        registered.append(name)
    _BACKEND_TOOLS[server_name] = registered   # remember which tools this backend contributed
    return registered


# --- a few safe demo tools (so the example runs without writing any) ---

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")


@register_tool(
    "calculator", "Evaluate a simple arithmetic expression (e.g. '2 * (3 + 4)').",
    {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
)
def calculator(expression: str) -> str:
    """Safely evaluate an arithmetic expression — no names, calls, or attribute access."""
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:  # noqa: BLE001 — tool errors are returned to the model, not raised
        return f"error: {e}"
