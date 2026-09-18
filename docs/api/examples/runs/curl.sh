#!/usr/bin/env bash
# Modulo API Example: Run Lifecycle — curl
#
# Demonstrates triggering a run, polling status, getting IO, and cancelling.
#
# Usage:
#   export MODULO_URL=http://localhost:8000
#   export MODULO_EMAIL=admin@example.com
#   export MODULO_PASSWORD=changeme
#   bash runs/curl.sh

set -euo pipefail

BASE_URL="${MODULO_URL:-http://localhost:8000}"
EMAIL="${MODULO_EMAIL:?MODULO_EMAIL is required}"
PASSWORD="${MODULO_PASSWORD:?MODULO_PASSWORD is required}"

echo "=== Login ==="
TOKEN=$(curl -s -X POST "$BASE_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg e "$EMAIL" --arg p "$PASSWORD" '{email: $e, password: $p}')" \
  | jq -r '.access_token')
AUTH="Authorization: Bearer $TOKEN"

echo ""
echo "=== Find First Pipeline ==="
PIPELINE_ID=$(curl -s "$BASE_URL/api/v1/pipelines?page=1&page_size=5" \
  -H "$AUTH" | jq -r '.items[0].id // empty')
if [[ -z "$PIPELINE_ID" ]]; then
  echo "No pipelines found. Create one first (see pipelines/ example)."
  exit 1
fi
echo "Using pipeline: $PIPELINE_ID"

echo ""
echo "=== Trigger Run ==="
RUN_RESP=$(curl -s -X POST "$BASE_URL/api/v1/runs" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d "$(jq -n --arg pid "$PIPELINE_ID" '{
    pipeline_id: $pid,
    input_payload: {pr_url: "https://github.com/example/org/pull/42"}
  }')")
RUN_ID=$(echo "$RUN_RESP" | jq -r '.run_id')
echo "Run ID: $RUN_ID, Status: $(echo "$RUN_RESP" | jq -r '.status')"

echo ""
echo "=== Poll Run Status ==="
for i in $(seq 1 15); do
  sleep 2
  STATUS=$(curl -s "$BASE_URL/api/v1/runs/$RUN_ID" -H "$AUTH" | jq -r '.status')
  echo "  [$i] status = $STATUS"
  if [[ "$STATUS" =~ ^(completed|failed|cancelled)$ ]]; then
    break
  fi
done

echo ""
echo "=== Get Run IO ==="
curl -s "$BASE_URL/api/v1/runs/$RUN_ID/io" -H "$AUTH" | jq

echo ""
echo "=== Cancel Run (if still running) ==="
curl -s -X POST "$BASE_URL/api/v1/runs/$RUN_ID/cancel" -H "$AUTH" | jq

echo ""
echo "=== Get WS Token ==="
WS_RESP=$(curl -s -X POST "$BASE_URL/api/v1/auth/ws-token" \
  -H "Content-Type: application/json" \
  -H "$AUTH" -d '{}')
WS_TOKEN=$(echo "$WS_RESP" | jq -r '.ws_token')
echo "WS token: ${WS_TOKEN:0:20}..."
echo "Connect: websocat $BASE_URL/api/v1/runs/$RUN_ID/ws?token=$WS_TOKEN"

echo ""
echo "Done."
