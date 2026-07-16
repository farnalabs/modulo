#!/bin/bash
set -euo pipefail

# Set up opencode credentials from APP_MODULO_OPENCODE_API_KEY if available
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
    echo "[modulo-wrap] OpenCode credentials written from APP_MODULO_OPENCODE_API_KEY"
fi

# modulo-wrap — Entrypoint wrapper for Modulo-compatible E2B agents
# Reads the agent command from environment or uses default, then wraps
# execution with telemetry capture and structured output contract.
#
# Environment variables (set by Modulo's sandbox_agent node):
#   MODULO_AGENT_COMMAND  — command to run (default: "opencode --output-json /home/user/output.json")
#   MODULO_RUN_ID         — Modulo run ID
#   MODULO_PIPELINE_ID    — Modulo pipeline ID
#   MODULO_ORG_ID         — Modulo org ID
#
# Contract:
#   Input:  /home/user/prompt.md — the rendered agent prompt
#   Output: /home/user/output.json — structured JSON result

PROMPT_FILE="${MODULO_PROMPT_FILE:-/home/user/prompt.md}"
OUTPUT_FILE="${MODULO_OUTPUT_FILE:-/home/user/output.json}"
AGENT_CMD="${MODULO_AGENT_COMMAND:-opencode --output-json /home/user/output.json}"

START_TIME=$(date +%s%N)

echo "[modulo-wrap] Running: $AGENT_CMD"
echo "[modulo-wrap] Prompt: $PROMPT_FILE"
echo "[modulo-wrap] Output: $OUTPUT_FILE"

# Run the agent
set +e
eval "$AGENT_CMD"
AGENT_EXIT_CODE=$?
set -e

END_TIME=$(date +%s%N)
WALL_CLOCK_MS=$(( (END_TIME - START_TIME) / 1000000 ))

echo "[modulo-wrap] Exit code: $AGENT_EXIT_CODE"
echo "[modulo-wrap] Wall clock: ${WALL_CLOCK_MS}ms"

# Build telemetry payload
TELEMETRY=$(cat << JSONEOF
{
  "_telemetry": {
    "wall_clock_time_ms": $WALL_CLOCK_MS,
    "exit_code": $AGENT_EXIT_CODE,
    "sandbox_template": "${E2B_TEMPLATE_ID:-unknown}",
    "agent_command": "$AGENT_CMD",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  }
}
JSONEOF
)

# If agent produced output.json, merge telemetry into it
if [ -f "$OUTPUT_FILE" ]; then
    echo "[modulo-wrap] Output file found — merging telemetry"
    python3 -c "
import json, os
with open('$OUTPUT_FILE') as f:
    output = json.load(f)
# Add telemetry (don't overwrite agent's own telemetry if present)
if '_telemetry' not in output:
    output['_telemetry'] = json.loads('''$TELEMETRY''')
# Ensure top-level status
if 'status' not in output:
    output['status'] = 'completed' if $AGENT_EXIT_CODE == 0 else 'failed'
with open('$OUTPUT_FILE', 'w') as f:
    json.dump(output, f, indent=2)
"
else
    echo "[modulo-wrap] No output file found — creating minimal output"
    echo "$TELEMETRY" | python3 -c "
import json, sys
output = json.load(sys.stdin)
output['status'] = 'completed' if $AGENT_EXIT_CODE == 0 else 'failed'
output['summary'] = 'Agent completed with exit code $AGENT_EXIT_CODE'
with open('$OUTPUT_FILE', 'w') as f:
    json.dump(output, f, indent=2)
"
fi

echo "[modulo-wrap] Done — exiting with $AGENT_EXIT_CODE"
exit $AGENT_EXIT_CODE
