# AgentGate — declare → generate → run a multi-agent app as a service (E13).
# Standalone repo: orchestration-as-code. DriftWatch (governance) ships from its own repo.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.source="https://github.com/graphsentinel/agentgate"
LABEL org.opencontainers.image.description="AgentGate — declare an agent org as code, generate + run + govern it (E13)"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# server → FastAPI/uvicorn (the /run service); codegen → langgraph/langchain (the generated app);
# mcp → fastmcp (external MCP tool binding).
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir "$(echo /tmp/*.whl)[server,codegen,mcp]" && rm -rf /tmp/*.whl

RUN useradd -u 10001 -m agentgate
USER 10001

# The AgenticArchitecture is mounted at AGENTGATE_SPEC_PATH (default /etc/agentgate/org.yaml) by the
# Helm chart (a ConfigMap). Set AGENTGATE_DYNAMIC=true for the runtime-gated dynamic graph.
EXPOSE 8000
ENTRYPOINT ["agentgate-server"]
