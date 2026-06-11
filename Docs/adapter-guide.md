# Writing a Runtime Adapter

DriftWatch governs *which* agent runtimes via config, not code (FR-8). Kagent and Goose
ship built-in; any MCP / OpenAI-tools / HTTP runtime is added by writing a small
adapter against the SDK — no rebuild of the detection core.

## The contract

Subclass `driftwatch.sdk.RuntimeAdapter` and implement `normalize`:

```python
from driftwatch.sdk import RuntimeAdapter, ToolCall

@RuntimeAdapter.register            # registers under `name` for spec.runtimes
class MyAgentAdapter(RuntimeAdapter):
    name = "my-agent"

    def normalize(self, raw) -> ToolCall:
        # turn one raw framework tool-call event into a normalized ToolCall
        return ToolCall(
            tool=raw["tool_name"],
            scope=raw.get("namespace", ""),
            arguments=raw.get("args", {}),
        )
```

That's the whole contract. The detection core only ever sees the normalized
`ToolCall` / `DecisionChain` — it never knows your framework exists.

## Wiring it in

Reference it from the policy:

```yaml
spec:
  runtimes:
    - name: my-agent
      adapter: custom            # resolves your registered class by name
      interceptor:
        port: 8080
        protocol: mcp            # mcp | openai-tools | http
```

## What you get for free

- `arg_schema_hash()` over the argument *shape* (keys + types) — argument-injection
  detection.
- catalog enrichment: if you pass a `catalog={tool: {"category":..,"risk":..}}`,
  category/risk are filled automatically.
- the full four-feature scoring (tool / scope / sequence / argSchemaHash) and the
  `gen_ai.agent.*` emission — you write ~10 lines, you inherit all of it.

## Built-in references

- `src/driftwatch/adapters/kagent.py` — dict-style tool calls
- `src/driftwatch/adapters/goose.py` — MCP-style `{name, parameters}`
- `src/driftwatch/adapters/custom_example.py` — OpenAI tool-call shape
