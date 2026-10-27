#!/usr/bin/env bash
# Post-start smoke check for a Modulo deployment (Docker Compose, FAR-2181).
#
# WHY THIS EXISTS
# A stack can be *almost* up and still look healthy. The API answers, login
# returns 200, `docker compose ps` shows every container running - and yet
# nothing can store a credential, because FERNET_KEY is not a valid Fernet key.
# `Settings` validates only its LENGTH (>= 32 bytes), so a longer placeholder
# boots fine and then fails later, quietly:
#
#   * startup.checkpointer_init_failed - `checkpoints` and
#     `checkpoint_migrations` are never created, and /healthz/ready reports
#     `checkpointer: degraded` while the endpoint still answers 200;
#   * the FIRST credential write fails - `POST /api/v1/model-backends`
#     returns 500 (the api_key is Fernet-encrypted at rest).
#
# Neither is visible to a login, a `docker compose ps`, or a bare health poll,
# so this script asserts the two things that actually prove the key works:
# a real credential round-trip, and the checkpointer schema existing.
#
# WHAT IT CHECKS (in order, stopping at the first failure)
#   1. the API answers and login succeeds;
#   2. /healthz/ready reports the `checkpointer` check `ok`;
#   3. `checkpoints` and `checkpoint_migrations` exist in Postgres;
#   4. creating a model backend - which encrypts its api_key with FERNET_KEY -
#      returns 201, and reading it back returns the same name;
#   5. cleanup: the model backend created in (4) is deleted again.
#
# USAGE
#   # root compose stack, API on localhost:8000, db service `db`
#   ./scripts/smoke-compose.sh
#
#   # quickstart layout: compose runs db-local/redis-local + the SAQ workers,
#   # the API runs on the host
#   ./scripts/smoke-compose.sh --compose-file docker-compose.local.yml --db-service db-local
#
#   # DB is not a compose service (host Postgres, managed Postgres, ...)
#   ./scripts/smoke-compose.sh --skip-db-check
#
#   # compose project started with an explicit `-p` name
#   ./scripts/smoke-compose.sh --project-name my-modulo
#
# EXIT CODES
#   0 every check passed; 1 a check failed (the failing check names the fix).
#
# Requirements: bash, curl, python3 (the repo's own prerequisite), and - unless
# --skip-db-check - the docker CLI able to `docker compose exec` the DB service.

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
EMAIL="${MODULO_SMOKE_EMAIL:-admin}"
PASSWORD="${MODULO_SMOKE_PASSWORD:-admin}"
COMPOSE_FILE="docker-compose.yml"
PROJECT_NAME=""
DB_SERVICE="db"
DB_USER="modulo"
DB_NAME="modulo"
SKIP_DB_CHECK=0

usage() {
  sed -n '/^# USAGE$/,/^# EXIT CODES$/p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) BASE_URL="${2:?--base-url needs a value}"; shift 2 ;;
    --email) EMAIL="${2:?--email needs a value}"; shift 2 ;;
    --password) PASSWORD="${2:?--password needs a value}"; shift 2 ;;
    --compose-file) COMPOSE_FILE="${2:?--compose-file needs a value}"; shift 2 ;;
    --project-name) PROJECT_NAME="${2:?--project-name needs a value}"; shift 2 ;;
    --db-service) DB_SERVICE="${2:?--db-service needs a value}"; shift 2 ;;
    --db-user) DB_USER="${2:?--db-user needs a value}"; shift 2 ;;
    --db-name) DB_NAME="${2:?--db-name needs a value}"; shift 2 ;;
    --skip-db-check) SKIP_DB_CHECK=1; shift ;;
    -h | --help) usage 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
done

BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

TOKEN=""
HTTP_CODE=""
HTTP_BODY=""

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }

# A skipped check is not a passed one - never print it as PASS.
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; }

fail() {
  printf '  \033[31mFAIL\033[0m %s\n' "$1" >&2
  if [[ -n ${2:-} ]]; then printf '       fix: %s\n' "$2" >&2; fi
  exit 1
}

# http METHOD PATH [JSON_BODY] -> HTTP_CODE / HTTP_BODY
http() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS --max-time 30 -o "$BODY_FILE" -w '%{http_code}' -X "$method" "${BASE_URL}${path}")
  if [[ -n "$TOKEN" ]]; then args+=(-H "Authorization: Bearer ${TOKEN}"); fi
  if [[ -n "$body" ]]; then args+=(-H 'Content-Type: application/json' --data "$body"); fi
  HTTP_CODE="$(curl "${args[@]}")" || fail "cannot reach ${BASE_URL}${path}" "is the stack up? check \`docker compose ps\`"
  HTTP_BODY="$(cat "$BODY_FILE")"
}

# json_get DOTTED.PATH -> prints the value ("" when the path is absent)
json_get() {
  local path="$1"
  HTTP_BODY="$HTTP_BODY" python3 - "$path" <<'PY'
import json, os, sys
value = json.loads(os.environ["HTTP_BODY"] or "null")
for part in sys.argv[1].split("."):
    if isinstance(value, dict):
        value = value.get(part)
    elif isinstance(value, list):
        value = value[int(part)]
    else:
        value = None
    if value is None:
        break
print("" if value is None else value)
PY
}

echo "Modulo compose smoke check -> ${BASE_URL}"

# 1. API reachable + login.
http POST /api/v1/auth/login "$(python3 -c '
import json, sys
print(json.dumps({"email": sys.argv[1], "password": sys.argv[2]}))' "$EMAIL" "$PASSWORD")"
if [[ "$HTTP_CODE" != "200" ]]; then
  fail "login as ${EMAIL} returned HTTP ${HTTP_CODE}" "check MODULO_USERS in your .env and \`docker compose logs backend\`"
fi
TOKEN="$(json_get access_token)"
if [[ -z "$TOKEN" ]]; then
  fail "login returned no access_token" "the login response shape changed - inspect ${HTTP_BODY}"
fi
pass "login as ${EMAIL} (HTTP 200)"

# 2. Readiness must report the checkpointer healthy - the first symptom of a
#    bad FERNET_KEY is this check going degraded while the endpoint still 200s.
http GET /healthz/ready
if [[ "$HTTP_CODE" != "200" ]]; then
  fail "/healthz/ready returned HTTP ${HTTP_CODE}" "the stack is not ready - \`curl -s ${BASE_URL}/healthz/ready | python3 -m json.tool\`"
fi
CHECKPOINTER_STATUS="$(json_get checks.checkpointer.status)"
if [[ "$CHECKPOINTER_STATUS" != "ok" ]]; then
  fail "readiness reports checkpointer '${CHECKPOINTER_STATUS:-missing}'" \
    "a Fernet key that cannot initialise the checkpointer is the usual cause - see \`docker compose logs backend | grep checkpointer_init_failed\`; regenerate FERNET_KEY (openssl rand -base64 32) and recreate the stack"
fi
pass "readiness checkpointer ok"

# 3. The checkpoint tables must exist. /healthz/ready can only tell you the
#    probe failed; this names what is missing.
if [[ "$SKIP_DB_CHECK" -eq 1 ]]; then
  skip "checkpoint tables (--skip-db-check: the DB assertions were not run)"
else
  COMPOSE_ARGS=(-f "$COMPOSE_FILE")
  if [[ -n "$PROJECT_NAME" ]]; then COMPOSE_ARGS+=(-p "$PROJECT_NAME"); fi
  TABLES="$(docker compose "${COMPOSE_ARGS[@]}" exec -T "$DB_SERVICE" \
    psql -U "$DB_USER" -d "$DB_NAME" -tAc \
    "SELECT coalesce(to_regclass('public.checkpoints')::text, '') || ',' || coalesce(to_regclass('public.checkpoint_migrations')::text, '');" \
    2>/dev/null | tr -d '[:space:]')" ||
    fail "could not query ${DB_SERVICE} (service '${DB_SERVICE}' in ${COMPOSE_FILE})" \
      "pass --db-service/--db-user/--db-name for your layout, or --skip-db-check"
  if [[ "$TABLES" != "checkpoints,checkpoint_migrations" ]]; then
    fail "checkpoint tables missing: got '${TABLES}'" \
      "the app never created them - see \`docker compose logs backend | grep checkpointer_init_failed\`"
  fi
  pass "checkpoint tables present (checkpoints, checkpoint_migrations)"
fi

# 4. A real credential round-trip: POST /api/v1/model-backends encrypts the
#    api_key with FERNET_KEY. This is the exact call that returned 500 on a
#    stack whose key only passed the length check.
SMOKE_NAME="smoke-check-$(date -u +%Y%m%d%H%M%S)-$$"
http POST /api/v1/model-backends "$(python3 -c '
import json, sys
print(json.dumps({
    "name": sys.argv[1],
    "display_name": "Compose smoke check",
    "provider": "custom",
    "model_id": "stub",
    "api_key": "not-a-real-credential",
}))' "$SMOKE_NAME")"
if [[ "$HTTP_CODE" != "201" ]]; then
  fail "creating a model backend returned HTTP ${HTTP_CODE} (expected 201)" \
    "FERNET_KEY is not a valid Fernet key, or the stack is unhealthy - \`docker compose logs backend | tail -50\`; generate a key with \`openssl rand -base64 32\`"
fi
BACKEND_ID="$(json_get id)"
pass "credential stored (model backend created, HTTP 201)"

# 5. Cleanup - leave the stack as we found it. A failure here is a warning, not
#    a smoke-check failure: the check's job is already done.
if [[ -n "$BACKEND_ID" ]]; then
  http DELETE "/api/v1/model-backends/${BACKEND_ID}"
  if [[ "$HTTP_CODE" == "204" || "$HTTP_CODE" == "200" ]]; then
    pass "smoke-check model backend removed"
  else
    printf '  WARN could not delete smoke-check model backend %s (HTTP %s) - remove it by hand\n' \
      "$BACKEND_ID" "$HTTP_CODE" >&2
  fi
fi

echo "Smoke check passed: login, checkpointer, checkpoint tables, credential round-trip."
