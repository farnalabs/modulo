#!/bin/bash
set -euo pipefail

PROMPT_FILE="${MODULO_PROMPT_FILE:-/home/user/prompt.md}"
OUTPUT_FILE="${MODULO_OUTPUT_FILE:-/home/user/output.json}"
PORT=9893
BASE_URL="http://127.0.0.1:${PORT}"

if [ -n "${APP_MODULO_OPENCODE_API_KEY:-}" ]; then
    OPENCODE_AUTH_DIR="${HOME}/.local/share/opencode"
    mkdir -p "${OPENCODE_AUTH_DIR}"
    cat > "${OPENCODE_AUTH_DIR}/auth.json" << EOF
{
  "opencode": {
    "type": "api",
    "key": "${APP_MODULO_OPENCODE_API_KEY}"
  },
  "opencode-go": {
    "type": "api",
    "key": "${APP_MODULO_OPENCODE_API_KEY}"
  }
}
EOF
fi

if [ ! -f "$PROMPT_FILE" ]; then
    echo "{\"status\":\"failed\",\"summary\":\"Prompt file not found: $PROMPT_FILE\"}" > "$OUTPUT_FILE"
    exit 1
fi

PROMPT_TEXT=$(cat "$PROMPT_FILE")

lildax serve --port "$PORT" &
SERVER_PID=$!

set +e
for i in $(seq 1 15); do
    HEALTH=$(curl -s --max-time 3 "${BASE_URL}/api/health" 2>/dev/null)
    if echo "$HEALTH" | grep -q '"healthy":true'; then
        echo "[lildax-review] Server ready after ${i}s"
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo "[lildax-review] Server failed to start"
        kill "$SERVER_PID" 2>/dev/null
        echo "{\"status\":\"failed\",\"summary\":\"lildax server did not start\"}" > "$OUTPUT_FILE"
        exit 1
    fi
    sleep 1
done
set -e

SESSION_RESP=$(lildax api v2.session.create --data "{}" 2>/dev/null)
SESSION_ID=$(echo "$SESSION_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])" 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    kill "$SERVER_PID" 2>/dev/null
    echo "{\"status\":\"failed\",\"summary\":\"Failed to create session\"}" > "$OUTPUT_FILE"
    exit 1
fi

echo "[lildax-review] Session: $SESSION_ID"

ESCAPED_PROMPT=$(echo "$PROMPT_TEXT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")
set +e
lildax api v2.session.prompt --param "sessionID=$SESSION_ID" --data "{\"prompt\":{\"text\":$ESCAPED_PROMPT}}" 2>/dev/null
PROMPT_EXIT=$?
set -e

if [ $PROMPT_EXIT -ne 0 ]; then
    kill "$SERVER_PID" 2>/dev/null
    echo "{\"status\":\"failed\",\"summary\":\"Failed to send prompt to lildax\"}" > "$OUTPUT_FILE"
    exit 1
fi

echo "[lildax-review] Prompt sent, waiting for response..."

OUTPUT_TEXT=""
set +e
for i in $(seq 1 30); do
    MESSAGES=$(lildax api v2.session.messages --param "sessionID=$SESSION_ID" 2>/dev/null)
    DATA=$(echo "$MESSAGES" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    msgs=d.get('data',[])
    for m in msgs:
        if m.get('role')=='assistant':
            print(m.get('text',''))
            break
except: pass
" 2>/dev/null)
    if [ -n "$DATA" ]; then
        OUTPUT_TEXT="$DATA"
        break
    fi
    sleep 2
done
set -e

if [ -z "$OUTPUT_TEXT" ]; then
    echo "{\"status\":\"completed\",\"summary\":\"No response from lildax (timed out)\",\"response\":\"\"}" > "$OUTPUT_FILE"
else
    echo "{\"status\":\"completed\",\"summary\":\"PR review completed\",\"response\":$(echo "$OUTPUT_TEXT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")}" > "$OUTPUT_FILE"
fi

kill "$SERVER_PID" 2>/dev/null
wait "$SERVER_PID" 2>/dev/null

echo "[lildax-review] Output written to $OUTPUT_FILE"
exit 0
