# Publishing AgentGate to GHCR (public)

AgentGate is a **standalone repo** — orchestration-as-code: declare an agent org → generate → run →
govern (E13). This publishes the AgentGate image (`Dockerfile`, entrypoint `agentgate-server`) and its
Helm chart to GHCR. DriftWatch (the governance/drift product) is a separate repo with its own
publishing doc — the two share only the `AgenticArchitecture` format + telemetry/`_meta` protocols.

Two artifacts, one registry (`ghcr.io`, owner = **graphsentinel**):

| Artifact | Reference |
|---|---|
| Container image (`agentgate-server`, `[interceptor,codegen,operator,mcp]`) | `ghcr.io/graphsentinel/agentgate:0.1.0` |
| Helm chart (OCI artifact) | `oci://ghcr.io/graphsentinel/charts/agentgate:0.1.0` |

Both start **private** and are flipped to **public** once; after that anyone can `helm install` from
the OCI chart and `podman pull` (or `docker pull`) the image with no auth.

> **Config is NOT baked into the image.** The agent org is `values.org` → a ConfigMap mounted at
> `/etc/agentgate/org.yaml`; `dynamic` and the LLM `env` are Helm values. The same image runs any org
> without rebuilding — edit values, `helm upgrade`, done.

> **Tag vs digest.** `:0.1.0` is a mutable convenience tag. For anything reproducible (a demo, a
> runbook) pin the immutable digest and install with `--set-string image.digest=sha256:…` — same
> mechanism as DriftWatch (see its doc's *Release immutability* section).

---

## Path A — manual, no GitHub Actions (recommended)

Local container tooling + `helm`. **Both `podman` and `docker` work** (examples use `podman`).

### 0. One-time: a PAT for registry login

GitHub → Settings → Developer settings → **Personal access tokens (classic)** → scopes
`write:packages` + `read:packages`:

```bash
export GHCR_PAT=ghp_xxxxxxxxxxxx
```

### 1. Build + push the image

```bash
echo "$GHCR_PAT" | podman login ghcr.io -u graphsentinel --password-stdin

podman build -f Dockerfile -t ghcr.io/graphsentinel/agentgate:0.1.0 .
podman push                          ghcr.io/graphsentinel/agentgate:0.1.0
```

> `Dockerfile` builds with the `codegen` extra (so the generated LangGraph app can run) and
> `ENTRYPOINT agentgate-server`, port 8000.

### (optional) Smoke-test the image locally before pushing

```bash
podman run --rm -p 8000:8000 \
  -v $PWD/examples/e13-orchestration-as-code/org.yaml:/etc/agentgate/org.yaml:ro \
  ghcr.io/graphsentinel/agentgate:0.1.0
# in another shell:
curl -s localhost:8000/ | jq                                   # agents + coordinator
curl -s -X POST localhost:8000/run -d '{"goal":"reverse a string"}' | jq
```

(No model needed: with no `DRIFTWATCH_LLM_PROVIDER` the agents run in deterministic stub mode, which
still proves the wiring. Set the LLM env to go live.)

### 2. Package + push the Helm chart (OCI)

```bash
helm registry login ghcr.io -u graphsentinel -p "$GHCR_PAT"

helm package deploy/helm/agentgate -d /tmp/agchart           # -> agentgate-0.1.0.tgz
helm push /tmp/agchart/agentgate-0.1.0.tgz oci://ghcr.io/graphsentinel/charts
```

### 3. One-time: flip both packages to Public

GitHub → **graphsentinel** org → **Packages**: set `agentgate` and `charts/agentgate` →
**Package settings → Change visibility → Public**.

---

## What a third party does (the whole point)

```bash
# install straight from the OCI registry — override the org / dynamic / LLM as needed
helm install agentgate oci://ghcr.io/graphsentinel/charts/agentgate --version 0.1.0 \
  --set dynamic=true \
  --set 'env[0].name=DRIFTWATCH_LLM_PROVIDER,env[0].value=ollama' \
  --set 'env[1].name=DRIFTWATCH_OLLAMA_HOST,env[1].value=http://host.k3d.internal:11434'

kubectl port-forward svc/agentgate-agentgate 8000:8000 &
curl -s -X POST localhost:8000/run -d '{"goal":"is 17 prime?"}' | jq
```

**Change the org** = edit `values.org` (or `-f my-values.yaml`, or `--set`) and `helm upgrade` — a
checksum annotation rolls the pod so the new spec is read. The image never changes.

- `dynamic=false` (default): static graph — creation-driven, undeclared hand-off impossible.
- `dynamic=true`: the orchestrator picks the next agent at run time; `check_delegation` gates it
  against the declared graph (novel-edge / cycle / scope-escalation → recorded + blocked).

---

## Verify it's really public

```bash
podman logout ghcr.io
podman pull ghcr.io/graphsentinel/agentgate:0.1.0                       # must succeed, no login
helm pull oci://ghcr.io/graphsentinel/charts/agentgate --version 0.1.0  # must succeed
```

## Version bumps

Image tag = chart `appVersion` (`0.1.0`); chart tag = `version` (`0.1.0`). Bump both, rebuild/push
the image (step 1), repackage/push the chart (step 2). Digest-pinning works as in DriftWatch
(`values.yaml` carries `image.digest: ""`).
