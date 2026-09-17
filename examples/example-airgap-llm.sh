#!/usr/bin/env bash
# example-airgap-llm.sh --- prove scandbox airgap: only the LLM path gets through.
#
# Wires a tiny fake LLM behind the real scandbox gateway + recorder, runs a real
# scandbox-pi agent container on the airgap network, and proves:
#   1. Agent CAN reach the LLM via the gateway (OpenAI-compatible API)
#   2. Agent CANNOT reach the internet or the LLM directly
#   3. Every interaction is hash-chain logged in plane1.jsonl
#
# Prerequisites:
#   ml podman                          # load podman
#   ../build-podman.sh --from docker   # build scandbox-gateway + scandbox-pi
#   podman build -t scandbox-tiny-llm -f tiny-llm/Dockerfile tiny-llm
#
# Usage:
#   ./example-airgap-llm.sh              # full test
#   ./example-airgap-llm.sh --keep       # leave containers up after
#   ./example-airgap-llm.sh --llm URL    # use a real LLM instead of tiny-llm
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

AIRGAP_NET="scandbox-airgap"
GW_NAME="scandbox-gw-test"
AGENT_NAME="scandbox-agent-test"
LLM_NAME="scandbox-tiny-llm"
LLM_IMAGE="scandbox-tiny-llm"
AGENT_IMAGE="scandbox-pi"
GW_IMAGE="scandbox-gateway"
AUDIT_ROOT="${HOME}/.local/state/scandbox"
RUN_ID="airgap-test-$(date +%s)"
AUDIT="${AUDIT_ROOT}/${RUN_ID}"
PLANE1="$AUDIT/plane1.jsonl"

KEEP=0
EXT_LLM=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --keep) KEEP=1; shift ;;
        --llm)  EXT_LLM="$2"; shift 2 ;;
        *) echo "usage: $0 [--keep] [--llm URL]" >&2; exit 1 ;;
    esac
done

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1" >&2; }

cleanup() {
    echo ""
    echo "=== cleanup ==="
    if [[ "$KEEP" -eq 1 ]]; then
        echo "  --keep: containers still running"
        echo "  exec:  podman exec $AGENT_NAME curl http://llm-gateway:8080/v1/models"
        echo "  logs:  podman logs $GW_NAME"
        echo "  stop:  podman rm -f $GW_NAME $AGENT_NAME $LLM_NAME"
    else
        podman rm -f "$GW_NAME" "$AGENT_NAME" 2>/dev/null || true
        [[ -z "$EXT_LLM" ]] && podman rm -f "$LLM_NAME" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---- preflight ---------------------------------------------------------------

for img in "$GW_IMAGE" "$AGENT_IMAGE"; do
    podman image exists "$img" 2>/dev/null || { echo "error: image '$img' not found" >&2; exit 1; }
done

mkdir -p "$AUDIT_ROOT"
mkdir -p "$AUDIT"

# --disable-dns: aardvark-dns needs dbus; we use --add-host instead
if ! podman network inspect "$AIRGAP_NET" &>/dev/null; then
    podman network create --internal --disable-dns "$AIRGAP_NET"
elif [[ "$(podman network inspect "$AIRGAP_NET" --format '{{.DNSEnabled}}')" == "true" ]]; then
    podman network rm -f "$AIRGAP_NET" 2>/dev/null || true
    podman network create --internal --disable-dns "$AIRGAP_NET"
fi

echo "=== scandbox airgap + LLM path test ==="
echo "  audit: $AUDIT"

# ---- 1. LLM -----------------------------------------------------------------

if [[ -n "$EXT_LLM" ]]; then
    LLM_URL="$EXT_LLM"
    echo ""
    echo "--- 1. using external LLM: $LLM_URL ---"
else
    echo ""
    echo "--- 1. start tiny-llm (default network) ---"
    podman rm -f "$LLM_NAME" 2>/dev/null || true
    podman run -d --name "$LLM_NAME" --network podman "$LLM_IMAGE"
    sleep 1
    LLM_IP=$(podman inspect "$LLM_NAME" --format '{{json .NetworkSettings.Networks}}' \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['podman']['IPAddress'])")
    LLM_URL="http://${LLM_IP}:8080"
    echo "  tiny-llm at $LLM_URL"
fi

# ---- 2. gateway recorder (dual-homed: airgap + podman) ----------------------

echo ""
echo "--- 2. start gateway recorder ---"
podman rm -f "$GW_NAME" 2>/dev/null || true
podman run -d --name "$GW_NAME" --network podman --dns=none \
    -e GATEWAY_LISTEN=:8080 \
    -e "GATEWAY_UPSTREAM=$LLM_URL" \
    -e GATEWAY_TLS_VERIFY=0 \
    -e GATEWAY_LOG=/logs/plane1.jsonl \
    -e GATEWAY_RUN_ID=airgap-test \
    -v "$AUDIT:/logs" \
    "$GW_IMAGE"
podman network connect "$AIRGAP_NET" "$GW_NAME"
sleep 1

GW_IP=$(podman inspect "$GW_NAME" --format '{{json .NetworkSettings.Networks}}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['$AIRGAP_NET']['IPAddress'])")
echo "  gateway at $GW_IP (dual-homed: $AIRGAP_NET + podman)"

# ---- 3. agent container (airgap only, stays running) ------------------------

echo ""
echo "--- 3. start agent container (airgap only) ---"
podman rm -f "$AGENT_NAME" 2>/dev/null || true
podman run -d --name "$AGENT_NAME" --network "$AIRGAP_NET" --dns=none \
    --add-host "llm-gateway:$GW_IP" \
    -e "OPENAI_BASE_URL=http://llm-gateway:8080/v1" \
    -e "OPENAI_API_KEY=scandbox" \
    --entrypoint sleep \
    "$AGENT_IMAGE" infinity
sleep 1

echo "  agent running (airgap only, gateway via --add-host)"
echo ""
echo "  topology:"
echo "    [$AGENT_NAME]  --airgap-->  [$GW_NAME $GW_IP]  --podman-->  [LLM $LLM_URL]"

# ---- 4. test: OpenAI API through gateway ------------------------------------

echo ""
echo "--- 4. test OpenAI API from inside agent ---"

echo ""
echo "[test] GET /v1/models..."
MODELS=$(podman exec "$AGENT_NAME" curl -sf --max-time 5 \
    -H "Authorization: Bearer scandbox" \
    http://llm-gateway:8080/v1/models 2>/dev/null || echo '{}')
MODEL_ID=$(echo "$MODELS" | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['id'])" 2>/dev/null || echo "")
if [[ -n "$MODEL_ID" ]]; then
    pass "/v1/models -> model '$MODEL_ID'"
else
    fail "/v1/models returned no model (got: $MODELS)"
fi

echo ""
echo "[test] GET /health..."
HEALTH=$(podman exec "$AGENT_NAME" curl -sf --max-time 5 \
    http://llm-gateway:8080/health 2>/dev/null || echo '{}')
if echo "$HEALTH" | grep -q "ok\|healthy\|status"; then
    pass "/health responded: $HEALTH"
else
    fail "/health no response (got: $HEALTH)"
fi

echo ""
echo "[test] POST /v1/chat/completions..."
COMPLETION=$(podman exec "$AGENT_NAME" curl -sf --max-time 10 \
    -X POST http://llm-gateway:8080/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer scandbox" \
    -d '{"model":"'"$MODEL_ID"'","messages":[{"role":"user","content":"What is 2+2? Reply with just the number."}]}' \
    2>/dev/null || echo '{}')
REPLY=$(echo "$COMPLETION" | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'])" 2>/dev/null || echo "")
if [[ -n "$REPLY" ]]; then
    pass "chat completion returned: $REPLY"
else
    fail "chat completion empty (got: $COMPLETION)"
fi

# ---- 5. test: airgap holds ---------------------------------------------------

echo ""
echo "--- 5. test airgap isolation ---"

echo ""
echo "[test] DNS resolve google.com (should fail)..."
DNS=$(podman exec "$AGENT_NAME" python3 -c "
import socket
try:
    socket.getaddrinfo('google.com', 443)
    print('RESOLVED')
except Exception as e:
    print(f'BLOCKED: {e}')
" 2>/dev/null || echo "BLOCKED: exec error")
if echo "$DNS" | grep -q "BLOCKED"; then
    pass "DNS blocked"
else
    fail "DNS resolved---airgap broken! ($DNS)"
fi

echo ""
echo "[test] TCP to 8.8.8.8:53 (should fail)..."
TCP=$(podman exec "$AGENT_NAME" python3 -c "
import socket
s = socket.socket(); s.settimeout(3)
try:
    s.connect(('8.8.8.8', 53)); print('CONNECTED')
except Exception as e:
    print(f'BLOCKED: {e}')
finally:
    s.close()
" 2>/dev/null || echo "BLOCKED: exec error")
if echo "$TCP" | grep -q "BLOCKED"; then
    pass "TCP to 8.8.8.8 blocked"
else
    fail "TCP to 8.8.8.8 succeeded---airgap broken! ($TCP)"
fi

if [[ -z "$EXT_LLM" ]]; then
    LLM_DIRECT_IP=$(echo "$LLM_URL" | sed 's|http://||;s|:.*||')
    echo ""
    echo "[test] direct access to LLM at $LLM_DIRECT_IP (should fail)..."
    DIRECT=$(podman exec "$AGENT_NAME" curl -sf --max-time 3 \
        "http://${LLM_DIRECT_IP}:8080/health" 2>/dev/null && echo "REACHED" || echo "BLOCKED")
    if [[ "$DIRECT" == "BLOCKED" ]]; then
        pass "direct LLM access blocked---must go through gateway"
    else
        fail "direct LLM access succeeded---airgap broken!"
    fi
fi

# ---- 6. verify recorder output ----------------------------------------------

echo ""
echo "--- 6. verify recorder output ---"

if [[ ! -f "$PLANE1" ]]; then
    fail "no plane1.jsonl"
else
    RECORDS=$(wc -l < "$PLANE1")
    pass "plane1.jsonl: $RECORDS records"

    echo ""
    echo "  captured records:"
    python3 -c "
import json
with open('$PLANE1') as f:
    for line in f:
        r = json.loads(line)
        ep = r.get('endpoint', '?')
        st = r.get('status', '?')
        m  = r.get('model', '-')
        c  = (r.get('completion') or '')[:60]
        h  = r['record_hash'][:12]
        print(f'    seq={r[\"seq\"]}  {ep:24s}  status={st}  model={m}')
        if c:
            print(f'      -> {c}')
"

    # verify input was captured
    INPUT_OK=$(python3 -c "
import json
with open('$PLANE1') as f:
    for line in f:
        r = json.loads(line)
        if r.get('endpoint') == 'chat.completions' and r.get('request'):
            msgs = r['request'].get('messages', [])
            if any('2+2' in (m.get('content','')) for m in msgs):
                print('yes'); break
    else:
        print('no')
")
    if [[ "$INPUT_OK" == "yes" ]]; then
        pass "request body captured (contains '2+2')"
    else
        fail "request body not captured"
    fi

    # verify output was captured
    OUTPUT_OK=$(python3 -c "
import json
with open('$PLANE1') as f:
    for line in f:
        r = json.loads(line)
        if r.get('endpoint') == 'chat.completions' and r.get('completion'):
            print('yes'); break
    else:
        print('no')
")
    if [[ "$OUTPUT_OK" == "yes" ]]; then
        pass "completion captured in log"
    else
        fail "completion not captured"
    fi

    # verify hash chain
    echo ""
    echo "[test] hash chain integrity..."
    VERIFY=$(podman run --rm -v "$AUDIT:/logs:ro" "$GW_IMAGE" -verify /logs/plane1.jsonl 2>&1 || true)
    if echo "$VERIFY" | grep -q "OK:"; then
        pass "hash chain intact: $(echo "$VERIFY" | grep 'OK:')"
    else
        fail "hash chain broken: $VERIFY"
    fi
fi

# ---- summary -----------------------------------------------------------------

echo ""
echo "======================================="
echo "  PASS: $PASS   FAIL: $FAIL"
echo "  audit: $AUDIT"
echo "======================================="
[[ "$FAIL" -eq 0 ]] && echo "ALL TESTS PASSED" || { echo "SOME TESTS FAILED"; exit 1; }
