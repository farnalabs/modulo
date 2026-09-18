#!/usr/bin/env bash
# Modulo API Example: Authentication (Login) — curl
#
# Demonstrates login, token refresh, and logout using curl.
#
# Usage:
#   export MODULO_URL=http://localhost:8000
#   export MODULO_EMAIL=admin@example.com
#   export MODULO_PASSWORD=changeme
#   bash auth-login/curl.sh

set -euo pipefail

BASE_URL="${MODULO_URL:-http://localhost:8000}"
EMAIL="${MODULO_EMAIL:?MODULO_EMAIL is required}"
PASSWORD="${MODULO_PASSWORD:?MODULO_PASSWORD is required}"

echo "=== Login ==="
LOGIN_RESP=$(curl -s -X POST "$BASE_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg e "$EMAIL" --arg p "$PASSWORD" '{email: $e, password: $p}')")

ACCESS_TOKEN=$(echo "$LOGIN_RESP" | jq -r '.access_token')
REFRESH_TOKEN=$(echo "$LOGIN_RESP" | jq -r '.refresh_token')
echo "Access token:  ${ACCESS_TOKEN:0:20}..."
echo "Refresh token: ${REFRESH_TOKEN:0:20}..."

echo ""
echo "=== Get Current User ==="
curl -s "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $ACCESS_TOKEN" | jq

echo ""
echo "=== Refresh Token ==="
REFRESH_RESP=$(curl -s -X POST "$BASE_URL/api/v1/auth/refresh" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg rt "$REFRESH_TOKEN" '{refresh_token: $rt}')")

NEW_ACCESS=$(echo "$REFRESH_RESP" | jq -r '.access_token')
NEW_REFRESH=$(echo "$REFRESH_RESP" | jq -r '.refresh_token')
echo "New access token:  ${NEW_ACCESS:0:20}..."
echo "New refresh token: ${NEW_REFRESH:0:20}..."

echo ""
echo "=== Logout ==="
curl -s -X POST "$BASE_URL/api/v1/auth/logout" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NEW_ACCESS" \
  -d "$(jq -n --arg rt "$NEW_REFRESH" '{refresh_token: $rt}')" | jq

echo ""
echo "Done."
