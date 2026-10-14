#!/usr/bin/env bash
# Modulo API Example: Human-in-the-Loop (HITL) — curl
#
# Demonstrates listing, claiming, and approving/rejecting HITL gates.
#
# Usage:
#   export MODULO_URL=http://localhost:8000
#   export MODULO_EMAIL=admin@example.com
#   export MODULO_PASSWORD=changeme
#   bash hitl/curl.sh

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
echo "=== List Pending Gates (org-wide) ==="
PENDING=$(curl -s "$BASE_URL/api/v1/hitl/pending" -H "$AUTH")
echo "$PENDING" | jq
GATE_COUNT=$(echo "$PENDING" | jq '.gates | length')
if [[ "$GATE_COUNT" -eq 0 ]]; then
  echo "No pending gates found."
  exit 0
fi

# Grab the first unclaimed gate
FIRST_GATE=$(echo "$PENDING" | jq '.gates | map(select(.claimed_by == null)) | .[0] // empty')
if [[ -z "$FIRST_GATE" ]]; then
  echo "No available (unclaimed) gates."
  exit 0
fi

RUN_ID=$(echo "$FIRST_GATE" | jq -r '.run_id')
GATE_ID=$(echo "$FIRST_GATE" | jq -r '.gate_id')
echo ""
echo "=== Gate Details ==="
echo "Run ID:  $RUN_ID"
echo "Gate ID: $GATE_ID"

echo ""
echo "=== Claim Gate ==="
CLAIM_RESP=$(curl -s -X POST "$BASE_URL/api/v1/runs/$RUN_ID/hitl/$GATE_ID/claim" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d '{"expiry_minutes": 10}')
echo "$CLAIM_RESP" | jq
CLAIM_TOKEN=$(echo "$CLAIM_RESP" | jq -r '.claim_token')

echo ""
echo "=== Approve Gate ==="
curl -s -X POST "$BASE_URL/api/v1/runs/$RUN_ID/hitl/$GATE_ID/approve" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d "$(jq -n --arg ct "$CLAIM_TOKEN" '{
    claim_token: $ct,
    notes: "Approved via curl example"
  }')" | jq

echo ""
echo "Done."
