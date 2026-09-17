#!/usr/bin/env bash
# llm-probe.sh---confirm an LLM backend is up and serving an OpenAI-compatible API.
#
# Usage:
#   ./llm-probe.sh [CONTAINER]        # probe a container's published :8080 (default: llm)
#   ./llm-probe.sh --url https://HOST:PORT   # probe a URL directly (remote endpoint)
#
# Tries HTTPS (accepts self-signed, -k) then falls back to HTTP. Reports the model id
# and native context. Exit 0 = serving, non-zero = down / not serving.
set -uo pipefail

URL=""
CONTAINER="llm"
if [[ "${1:-}" == "--url" ]]; then
  URL="${2:?--url needs a value}"
else
  CONTAINER="${1:-llm}"
fi

if [[ -z "$URL" ]]; then
  if ! docker ps --filter "name=^/${CONTAINER}$" --format '{{.Names}}' | grep -q .; then
    echo "✗ container '${CONTAINER}' is not running"; exit 1
  fi
  HP=$(docker port "$CONTAINER" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')
  [[ -z "$HP" ]] && { echo "✗ '${CONTAINER}' has no published :8080 port"; exit 1; }
  URL="https://localhost:${HP}"
  echo "container '${CONTAINER}': host :${HP} → container :8080"
fi

fetch() {  # $1 = path; try HTTPS (self-signed ok), then HTTP
  local out
  out=$(curl -sk --max-time 8 "${URL}$1" 2>/dev/null)
  [[ -z "$out" ]] && out=$(curl -s --max-time 8 "${URL/https:/http:}$1" 2>/dev/null)
  printf '%s' "$out"
}

model=$(fetch /v1/models | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print((d.get('data') or d.get('models'))[0]['id'])" 2>/dev/null)
if [[ -z "$model" ]]; then
  echo "✗ no OpenAI-compatible response from ${URL}/v1/models"; exit 1
fi
ctx=$(fetch /props | python3 -c \
  "import sys,json; print(json.load(sys.stdin).get('default_generation_settings',{}).get('n_ctx',''))" 2>/dev/null)
echo "✓ serving @ ${URL}---model: ${model}${ctx:+  (n_ctx ${ctx})}"
