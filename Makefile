.PHONY: install test lint clean
PY ?= python3

install:        ## editable install with all extras
	$(PY) -m pip install -e ".[server,codegen,mcp,dev]"

test:           ## run the suite (codegen + runtime + contract)
	PYTHONPATH=src $(PY) -m pytest tests/ -q

lint:
	ruff check src tests
	mypy src/agentgate || true

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache .mypy_cache

# --- example: generate + run an AgenticArchitecture (E13) ---
#   agentgate generate examples/e13-orchestration-as-code/org.yaml
#   AGENTGATE_LLM_PROVIDER=ollama agentgate run examples/e13-orchestration-as-code/org.yaml \
#       --goal "investigate the latency spike"
# The HTTP /run service is `agentgate-server` (Helm: deploy/helm/agentgate).
