.PHONY: install test lint eval clean demo

PY ?= python3

install:        ## editable install with all dev extras
	$(PY) -m pip install -e ".[all]"

test:           ## run functional suite (TC-F-*)
	$(PY) -m pytest

lint:
	ruff check src tests
	mypy src/driftwatch || true

eval:           ## run drift dataset -> recall / FP-rate / p95 / inverse-scaling (+ results/)
	PYTHONPATH=src $(PY) -m driftwatch.cli eval --dataset evaluation/datasets/drift.jsonl --out evaluation/results

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache .mypy_cache

# --- demo ---
# The k3d demo has its own Makefile (cluster-up / obs-up / deploy / demo-1..5):
#   make -C examples/k3d-cluster-demo <target>
# `make demo` is a shortcut for the standalone five-scenario run (no cluster needed).
demo:           ## run all five drift scenarios standalone (delegates to the demo Makefile)
	$(MAKE) -C examples/k3d-cluster-demo demo-all
