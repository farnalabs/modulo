#!/usr/bin/env bash
# Modulo API Example: Library Primitive Management — curl
#
# Demonstrates browsing, searching, previewing, and adapting library primitives.
#
# Usage:
#   export MODULO_URL=http://localhost:8000
#   export MODULO_EMAIL=admin@example.com
#   export MODULO_PASSWORD=changeme
#   bash library/curl.sh

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
echo "=== Browse Library ==="
curl -s "$BASE_URL/api/v1/libraries?page=1&page_size=20" -H "$AUTH" | jq

echo ""
echo "=== Search Primitives (type=agent, search=review) ==="
curl -s "$BASE_URL/api/v1/libraries?primitive_type=agent&search=review&page=1&page_size=10" \
  -H "$AUTH" | jq

echo ""
echo "=== Get First Primitive Detail ==="
PRIMITIVE_ID=$(curl -s "$BASE_URL/api/v1/libraries?page=1&page_size=1" \
  -H "$AUTH" | jq -r '.items[0].id // empty')

if [[ -n "$PRIMITIVE_ID" ]]; then
  echo "Primitive ID: $PRIMITIVE_ID"
  curl -s "$BASE_URL/api/v1/libraries/$PRIMITIVE_ID" -H "$AUTH" | jq

  echo ""
  echo "=== Copy-to-Adapt ==="
  curl -s -X POST "$BASE_URL/api/v1/libraries/$PRIMITIVE_ID/adapt" \
    -H "Content-Type: application/json" \
    -H "$AUTH" -d '{}' | jq

  echo ""
  echo "=== Submit Rating ==="
  curl -s -X POST "$BASE_URL/api/v1/libraries/$PRIMITIVE_ID/ratings" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d '{"thumbs_up": true, "comment": "Very useful primitive!"}' | jq
else
  echo ""
  echo "=== Create a Primitive ==="
  curl -s -X POST "$BASE_URL/api/v1/libraries" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d '{
      "name": "Code Review Agent",
      "primitive_type": "agent",
      "slug": "code-review-agent",
      "description": "Reviews PR code changes",
      "content_json": {
        "prompt_template": "Review the following PR diff: {{diff}}",
        "input_schema": {"type": "object", "properties": {"diff": {"type": "string"}}}
      }
    }' | jq
fi

echo ""
echo "Done."
