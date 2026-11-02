#!/usr/bin/env bash
# Modulo API Example: Authentication (Login) - curl
#
# Demonstrates login, token refresh, and logout using curl.
#
# Authentication model (FAR-1197):
#   - The access token is in the login JSON body as `.access_token`.
#   - The refresh token is set ONLY as the httpOnly `modulo_refresh` cookie;
#     it never appears in a response body.
#   - `/auth/refresh` and `/auth/logout` take no request body. They read the
#     cookie and require a double-submit CSRF header: the JS-readable
#     `XSRF-TOKEN` cookie echoed in `X-CSRF-Token`.
#   - curl does not store cookies by itself, so this example uses a cookie jar
#     (`-c` writes it, `-b` sends it).
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

JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

# Read one cookie value from the Netscape-format cookie jar curl writes.
cookie_value() {
  awk -v name="$1" '$6 == name { print $7 }' "$JAR" | tail -n 1
}

echo "=== Login ==="
LOGIN_RESP=$(curl -s -c "$JAR" -X POST "$BASE_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg e "$EMAIL" --arg p "$PASSWORD" '{email: $e, password: $p}')")

ACCESS_TOKEN=$(echo "$LOGIN_RESP" | jq -r '.access_token')
echo "Access token:  ${ACCESS_TOKEN:0:20}..."
echo "Cookies set:   $(awk 'NF >= 7 { print $6 }' "$JAR" | sort -u | tr '\n' ' ')"

echo ""
echo "=== Get Current User ==="
curl -s "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $ACCESS_TOKEN" | jq

echo ""
echo "=== Refresh Token ==="
# No body: the server reads the modulo_refresh cookie. The X-CSRF-Token header
# must match the XSRF-TOKEN cookie sent with the same request.
CSRF_TOKEN=$(cookie_value XSRF-TOKEN)
REFRESH_RESP=$(curl -s -b "$JAR" -c "$JAR" -X POST "$BASE_URL/api/v1/auth/refresh" \
  -H "X-CSRF-Token: $CSRF_TOKEN")

NEW_ACCESS=$(echo "$REFRESH_RESP" | jq -r '.access_token')
echo "New access token: ${NEW_ACCESS:0:20}..."

echo ""
echo "=== Logout ==="
# The refresh response rotated both cookies, so read XSRF-TOKEN again.
CSRF_TOKEN=$(cookie_value XSRF-TOKEN)
curl -s -b "$JAR" -c "$JAR" -X POST "$BASE_URL/api/v1/auth/logout" \
  -H "Authorization: Bearer $NEW_ACCESS" \
  -H "X-CSRF-Token: $CSRF_TOKEN" | jq

echo ""
echo "Done."
