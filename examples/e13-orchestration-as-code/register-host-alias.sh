#!/usr/bin/env bash
# Portable host access for k3d: make `host.k3d.internal` resolve from inside the cluster so
# pods (e.g. a Kagent agent) can reach Ollama by a STABLE NAME — no hardcoded IP in any
# manifest. Re-run-safe (idempotent). Works on any k3d cluster and on any Ollama location:
#
#   - LOCAL Ollama (on the k3d host, the default): the host gateway IP is discovered from the
#     node's default route at run time, so manifests stay IP-free and portable across machines.
#
#   - REMOTE Ollama (a separate server): pass its address. If it's an IP it's aliased directly;
#     if it's a DNS name it's resolved (via getent/host/python) to an IP and aliased — so the
#     in-cluster name `host.k3d.internal` always points at wherever Ollama actually runs, and
#     every manifest keeps using the same name regardless of where Ollama lives.
#
# Usage:
#   ./register-host-alias.sh [cluster-name]            # LOCAL host Ollama (default)
#   OLLAMA_HOST=10.0.0.42 ./register-host-alias.sh     # REMOTE Ollama by IP
#   OLLAMA_HOST=ollama.corp.example ./register-host-alias.sh   # REMOTE Ollama by DNS name
#   ALIAS=ollama.internal OLLAMA_HOST=... ./register-host-alias.sh   # custom alias name
set -euo pipefail

CLUSTER="${1:-driftwatch-demo}"
SERVER_NODE="k3d-${CLUSTER}-server-0"
ALIAS="${ALIAS:-host.k3d.internal}"
OLLAMA_HOST="${OLLAMA_HOST:-}"

resolve_ip() {  # best-effort hostname → IPv4
  local h="$1" ip=""
  ip="$(getent ahostsv4 "$h" 2>/dev/null | awk '{print $1; exit}')" && [ -n "$ip" ] && { echo "$ip"; return; }
  ip="$(python3 -c 'import socket,sys; print(socket.gethostbyname(sys.argv[1]))' "$h" 2>/dev/null)" && [ -n "$ip" ] && { echo "$ip"; return; }
  return 1
}

if [ -z "$OLLAMA_HOST" ]; then
  echo "== mode: LOCAL host Ollama — discover the k3d host gateway from the node default route =="
  TARGET_IP="$(docker exec "$SERVER_NODE" ip route 2>/dev/null | awk '/default/{print $3; exit}')"
  [ -n "$TARGET_IP" ] || { echo "FATAL: could not determine host gateway from $SERVER_NODE"; exit 1; }
  echo "   host gateway = $TARGET_IP  (host-local; manifests stay IP-free and portable)"
elif printf '%s' "$OLLAMA_HOST" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "== mode: REMOTE Ollama by IP =="
  TARGET_IP="$OLLAMA_HOST"
  echo "   remote Ollama IP = $TARGET_IP"
else
  echo "== mode: REMOTE Ollama by DNS name — resolving $OLLAMA_HOST =="
  TARGET_IP="$(resolve_ip "$OLLAMA_HOST")" || {
    echo "FATAL: could not resolve $OLLAMA_HOST to an IPv4 address."
    echo "       If it is only resolvable inside the cluster's upstream DNS, point Kagent at"
    echo "       http://$OLLAMA_HOST:11434 directly and skip this alias."
    exit 1; }
  echo "   $OLLAMA_HOST -> $TARGET_IP"
fi

echo "== patch CoreDNS NodeHosts: $ALIAS -> $TARGET_IP (idempotent) =="
CUR="$(kubectl -n kube-system get cm coredns -o jsonpath='{.data.NodeHosts}')"
NEW="$(printf '%s\n' "$CUR" | grep -v " ${ALIAS}\$" || true)"
NEW="$(printf '%s\n%s %s\n' "$NEW" "$TARGET_IP" "$ALIAS")"
kubectl -n kube-system patch cm coredns --type merge \
  -p "$(python3 -c 'import json,sys; print(json.dumps({"data":{"NodeHosts":sys.argv[1]}}))' "$NEW")"

echo "== restart CoreDNS so it reloads NodeHosts =="
kubectl -n kube-system rollout restart deploy/coredns
kubectl -n kube-system rollout status deploy/coredns --timeout=60s

echo "== done. In-cluster pods can reach Ollama by name: http://$ALIAS:11434 =="
echo "   (alias '$ALIAS' -> $TARGET_IP). Re-run this script if Ollama moves."
