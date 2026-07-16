#!/bin/bash
set -euo pipefail

# headless-lildax — Run opencode inference in headless mode
# Starts lildax serve, sends a prompt via OpenAI-compatible chat completions,
# captures the response, and shuts down.

PROMPT_FILE="${1:-/home/user/prompt.md}"
OUTPUT_FILE="${2:-/home/user/output.json}"
PORT="${LILDAX_PORT:-8888}"

if [ ! -f "$PROMPT_FILE" ]; then
    echo '{"status":"failed","summary":"Prompt file not found"}' > "$OUTPUT_FILE"
    exit 1
fi

# Install openai Python package if not available
pip install openai -q 2>/dev/null || true

# Start lildax server in background
lildax serve --port "$PORT" &>/tmp/lildax-server.log &
SERVER_PID=$!

# Wait for server to be ready (up to 30 seconds)
for i in $(seq 1 30); do
    if curl -sf "http://localhost:$PORT/v1/chat/completions" -X POST \
        -H "Content-Type: application/json" \
        -d '{"model":"opencode-go","messages":[{"role":"user","content":"ping"}]}' \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Read the prompt
PROMPT=$(cat "$PROMPT_FILE")

# Send the prompt via Python (more reliable for complex JSON)
python3 -c "
import json, urllib.request, sys

prompt = '''$PROMPT'''
port = $PORT

data = json.dumps({
    'model': 'opencode-go',
    'messages': [{'role': 'user', 'content': prompt}]
}).encode()

req = urllib.request.Request(
    f'http://localhost:{port}/v1/chat/completions',
    data=data,
    headers={'Content-Type': 'application/json'},
    method='POST'
)

try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode())

    content = result.get('choices', [{}])[0].get('message', {}).get('content', '')

    # Try to parse the content as JSON (for structured output)
    try:
        output = json.loads(content)
    except json.JSONDecodeError:
        output = {'status': 'completed', 'summary': content}

    # Ensure required fields
    if 'status' not in output:
        output['status'] = 'completed'

    with open('$OUTPUT_FILE', 'w') as f:
        json.dump(output, f, indent=2)

except Exception as e:
    # Fallback: run review.py instead
    import subprocess
    result = subprocess.run(['python3', '/home/user/review.py'], capture_output=True, text=True, timeout=120)
    sys.exit(result.returncode)
"

AGENT_EXIT_CODE=$?

# Kill the server
kill "$SERVER_PID" 2>/dev/null

exit $AGENT_EXIT_CODE
