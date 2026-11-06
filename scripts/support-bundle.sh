#!/usr/bin/env bash
# =============================================================================
# Modulo support bundle - Docker Compose collector (FAR-2306)
# =============================================================================
#
# WHY THIS EXISTS
# Docker Compose is the documented five-minute demo, so it is the first thing a
# cold prospect runs - and it had no diagnostic story at all. No log volume is
# mounted for the backend or the workers, so every log line disappears the
# moment a container is recreated or the stack is brought down. `modulo doctor`
# is launcher-scoped and needs a data dir, so it does not apply here.
#
# This script captures what is still observable at the moment of failure, from
# the Docker CLI only, and writes ONE zip the user can inspect and send:
#
#   * docker-level facts (always collected, even with the stack down):
#       compose ps -a, per-container state/health/restart count/exit code,
#       image tags + digests, compose files in use (paths + hashes, never
#       contents), project name, resolved service list, volume NAMES only,
#       bounded and repeat-collapsed container logs, host facts + clock.
#   * app-level facts (collected only when the app container is reachable):
#       the in-container `python -m modulo.support.bundle` bundle, merged into
#       the same zip. The HTTP API is never called - it may be the thing that
#       is broken.
#
# CONTRACT WITH THE BUNDLE-FORMAT MODULE (FAR-2305)
# `python -m modulo.support.bundle --out <path> --json` inside the app
# container is the single source of truth for the format: the
# `bundle_format_version`, the `MANIFEST.txt`/`report.json` shape, the redaction
# layers and the ref line. This script does NOT reimplement redaction. It
# merges the module's members into a composite bundle and writes its own root
# MANIFEST.txt/report.json that list every member and its provenance. When the
# module is present its own MANIFEST.txt/report.json are preserved under
# `app/` (they are superseded by the composite ones, never dropped). When the
# module is absent the run still succeeds and records `missing: ["app"]`.
#
# CREDENTIAL POSTURE (by construction)
#   * `.env`, `secrets.json` and any credential file are never opened or copied.
#   * `docker compose config` is never run without a selector - the bare form
#     interpolates and prints env values. Only `config --services` and
#     `config --volumes` are used, which print names only.
#   * `docker info` is read through `--format` for selected fields only.
#   * no docker-socket mount, no sidecar, no new service, no open port,
#     no Farnalabs endpoint, no upload.
#   * docker-level log members are NEVER shipped verbatim. `docker compose logs`
#     output is piped through the module's own redaction (the same
#     `python -m modulo.support.bundle --redact-stdin` gate app-level members
#     use), run in a one-off container of the app image so it does not need the
#     stack to be up. If that filter cannot run, the log member is WITHHELD and
#     the reason is recorded - withholding is correct, leaking is not. A bash
#     regex scrub is never substituted for the module's redaction.
#   * the closing message tells the user to open the bundle before sending it.
#
# USAGE
#   ./scripts/support-bundle.sh                    # bundle in the repo root
#   ./scripts/support-bundle.sh --out /tmp/x.zip   # explicit destination
#   ./scripts/support-bundle.sh --no-app           # skip the in-container pass
#   ./scripts/support-bundle.sh --compose-file docker-compose.local.yml \
#       --project-name my-modulo --app-service backend
#   ./scripts/support-bundle.sh --tail 1000        # log lines per service
#
#   Run `./scripts/support-bundle.sh --help` for the full option list.
#
# EXIT CODES
#   0 a bundle was written (even when the stack is down)
#   1 the bundle could not be written (the reason names the failing step)
#   2 bad usage
#
# Requirements: bash, docker (with the compose plugin), zip. `unzip` is needed
# to merge the app bundle flat; without it the app bundle is nested as
# `app/modulo-support.zip` and the manifest says so.

set -euo pipefail

readonly SCRIPT_NAME="support-bundle"
#: The bundle format version this collector emits when it has to synthesise a
#: bundle without the in-container module. FAR-2305 owns this number; when the
#: module is reachable its own value wins (read from its report.json).
readonly DEFAULT_FORMAT_VERSION=1
#: Format requirement: the compressed bundle must stay at or under 5 MB.
readonly SIZE_CAP_BYTES=$((5 * 1024 * 1024))
#: Per-service log ceilings applied after repeat-collapsing.
readonly MAX_LOG_LINES=2000
readonly MAX_LOG_BYTES=$((256 * 1024))
readonly SOURCE_DOCKER="docker"
readonly SOURCE_APP="app"
readonly REDACTION_DOCKER_OUTPUT="verbatim (docker CLI output)"
readonly REDACTION_APP_MODULE="module (modulo.support.bundle)"

OUT=""
COMPOSE_FILE=""
PROJECT_NAME=""
APP_SERVICE="backend"
TAIL_LINES=400
COLLECT_APP=1

usage() {
  # Builtins only: --help must work on a machine with nothing else installed.
  printf '%s\n' \
    "USAGE" \
    "  ./scripts/support-bundle.sh [options]" \
    "" \
    "  --out <path>           destination zip (default: <repo root>/modulo-support-bundle-<UTC>.zip;" \
    "                         a directory means <dir>/modulo-support-bundle-<UTC>.zip)" \
    "  --compose-file <file>  compose file to inspect (default: <repo root>/docker-compose.yml)" \
    "  --project-name <name>  compose project name when it was started with an explicit -p" \
    "  --app-service <name>   service holding the app (default: backend)" \
    "  --tail <n>             log lines per service before collapsing repeats (default: 400)" \
    "  --no-app               skip the in-container app bundle (docker-level facts only)" \
    "  -h, --help             show this help" \
    "" \
    "Exit codes: 0 a bundle was written (even when the stack is down); 1 the bundle" \
    "could not be written; 2 bad usage."
}

die() {
  printf '%s: %s\n' "$SCRIPT_NAME" "$*" >&2
  exit 1
}

usage_error() {
  printf '%s: %s (try --help)\n' "$SCRIPT_NAME" "$*" >&2
  exit 2
}

progress() {
  printf '%s: %s\n' "$SCRIPT_NAME" "$*" >&2
}

# ---------------------------------------------------------------------------
# Argument parsing (no external commands, so --help and the dependency checks
# work even with a stripped PATH)
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      [[ $# -ge 2 ]] || usage_error "--out needs a value"
      OUT="$2"
      shift 2
      ;;
    --compose-file)
      [[ $# -ge 2 ]] || usage_error "--compose-file needs a value"
      COMPOSE_FILE="$2"
      shift 2
      ;;
    --project-name)
      [[ $# -ge 2 ]] || usage_error "--project-name needs a value"
      PROJECT_NAME="$2"
      shift 2
      ;;
    --app-service)
      [[ $# -ge 2 ]] || usage_error "--app-service needs a value"
      APP_SERVICE="$2"
      shift 2
      ;;
    --tail)
      [[ $# -ge 2 ]] || usage_error "--tail needs a value"
      TAIL_LINES="$2"
      shift 2
      ;;
    --no-app) COLLECT_APP=0; shift ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage_error "unknown argument: $1"
      ;;
  esac
done

case "$TAIL_LINES" in
  '' | *[!0-9]*) usage_error "--tail must be a non-negative integer" ;;
  *) ;;
esac

# ---------------------------------------------------------------------------
# Dependency checks - a missing tool is a clear, named failure, never a
# half-written bundle.
# ---------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 ||
  die "docker is required but was not found on PATH. Install Docker (with the compose plugin) and re-run."
command -v zip >/dev/null 2>&1 ||
  die "zip is required but was not found on PATH. Install zip (e.g. 'apt-get install zip' or 'brew install zip') and re-run."
if ! command -v unzip >/dev/null 2>&1; then
  progress "warning: unzip is not installed - an app bundle will be nested instead of merged flat"
fi

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

abspath() {
  local path="$1"
  case "$path" in
    /*) printf '%s' "$path" ;;
    *) printf '%s/%s' "$(pwd -P)" "$path" ;;
  esac
}

if [[ -z "$COMPOSE_FILE" ]]; then
  COMPOSE_FILE="$REPO_ROOT/docker-compose.yml"
else
  COMPOSE_FILE="$(abspath "$COMPOSE_FILE")"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -z "$OUT" ]]; then
  OUT="$REPO_ROOT/modulo-support-bundle-$STAMP.zip"
elif [[ -d "$OUT" ]]; then
  OUT="${OUT%/}/modulo-support-bundle-$STAMP.zip"
else
  OUT="$(abspath "$OUT")"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/modulo-support-bundle.XXXXXX")"
STAGE="$WORK/stage"
MEMBERS="$STAGE/members"
REGISTRY="$WORK/members.tsv"
MISSING_FILE="$WORK/missing.tsv"
NOTES_FILE="$WORK/notes.tsv"
TMP_ZIP="$WORK/partial.zip"
mkdir -p "$MEMBERS"
: >"$REGISTRY"
: >"$MISSING_FILE"
: >"$NOTES_FILE"

cleanup() {
  # A partial archive must never be mistaken for a complete one: the temp zip
  # lives next to the destination (so the final move is atomic), outside WORK.
  if [[ -n "${TMP_ZIP:-}" ]]; then
    rm -f "$TMP_ZIP" 2>/dev/null || true
  fi
  rm -rf "${WORK:-}"
}
trap cleanup EXIT

note() {
  printf '%s\t%s\n' "$1" "$2" >>"$NOTES_FILE"
}

record_missing() {
  printf '%s\t%s\n' "$1" "$2" >>"$MISSING_FILE"
}

register_member() {
  # rel <TAB> description <TAB> source <TAB> redaction
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >>"$REGISTRY"
}

file_size() {
  local path="$1"
  if stat -c %s "$path" >/dev/null 2>&1; then
    stat -c %s "$path"
  else
    stat -f %z "$path"
  fi
}

human_size() {
  local bytes="$1"
  if (( bytes >= 1048576 )); then
    awk -v b="$bytes" 'BEGIN { printf "%.1f MiB", b / 1048576 }'
  elif (( bytes >= 1024 )); then
    awk -v b="$bytes" 'BEGIN { printf "%.1f KiB", b / 1024 }'
  else
    printf '%s B' "$bytes"
  fi
}

sha256_of() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | cut -d' ' -f1
  else
    printf 'unavailable'
  fi
}

json_escape() {
  local value="$1"
  # Single-line, quote-safe value for the hand-written report.json.
  printf '%s' "$value" | tr '\n\t' '  ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

COMPOSE_ARGS=(-f "$COMPOSE_FILE")
if [[ -n "$PROJECT_NAME" ]]; then
  COMPOSE_ARGS+=(-p "$PROJECT_NAME")
fi

compose() {
  docker compose "${COMPOSE_ARGS[@]}" "$@"
}

# collect_member <rel> <description> <source> <redaction> -- <cmd...>
# Runs the command with stderr merged into the member (the failure text IS the
# diagnostic) and records a note when it fails. Never aborts the run.
collect_member() {
  local rel="$1" desc="$2" source="$3" redaction="$4"
  shift 4
  [[ "${1:-}" == "--" ]] && shift
  local dest="$MEMBERS/$rel"
  mkdir -p "$(dirname "$dest")"
  local rc=0
  "$@" >"$dest" 2>&1 || rc=$?
  if (( rc != 0 )); then
    {
      printf '\n[%s] command failed (exit %d): %s\n' "$SCRIPT_NAME" "$rc" "$*"
      printf '[%s] the failure above is part of the diagnostic record\n' "$SCRIPT_NAME"
    } >>"$dest"
    note "$rel" "collector command failed (exit $rc)"
  fi
  register_member "$rel" "$desc" "$source" "$redaction"
  return 0
}

# ---------------------------------------------------------------------------
# Logs: bounded per service, consecutive identical lines collapsed so a crash
# loop cannot fill the bundle.
# ---------------------------------------------------------------------------
collapse_logs() {
  awk -v max_lines="$MAX_LOG_LINES" -v max_bytes="$MAX_LOG_BYTES" '
    function emit(text,   n) {
      if (emitted >= max_lines) { truncated = 1; return }
      n = length(text) + 1
      if (bytes + n > max_bytes) { truncated = 1; return }
      print text
      emitted++
      bytes += n
    }
    {
      if (seen && $0 == prev) { count++; next }
      if (seen) {
        if (count > 1) emit(sprintf("%s [repeated %d times]", prev, count))
        else emit(prev)
      }
      prev = $0
      count = 1
      seen = 1
    }
    END {
      if (seen) {
        if (count > 1) emit(sprintf("%s [repeated %d times]", prev, count))
        else emit(prev)
      }
      if (truncated) {
        print "[collector] log truncated: capped at " max_lines " lines / " max_bytes " bytes"
      }
    }
  '
}

collect_logs() {
  local service="$1"
  local safe
  safe="$(printf '%s' "$service" | tr -c 'A-Za-z0-9_.-' '_')"
  local rel="docker/logs/${safe}.log.tail"
  local dest="$MEMBERS/$rel"
  local raw="$WORK/raw-${safe}.log"
  local collapsed="$WORK/collapsed-${safe}.log"
  local filtered="$WORK/filtered-${safe}.log"
  local rc=0
  compose logs --no-color --tail "$TAIL_LINES" "$service" >"$raw" 2>"$WORK/logs.err" || rc=$?
  collapse_logs <"$raw" >"$collapsed"
  if (( rc != 0 )); then
    {
      printf '\n[%s] docker compose logs %s failed (exit %d):\n' "$SCRIPT_NAME" "$service" "$rc"
      sed -n '1,3p' "$WORK/logs.err"
    } >>"$collapsed"
    note "$rel" "docker compose logs failed (exit $rc)"
  fi

  # Never ship a docker-level log verbatim: it is raw engine output and can
  # contain anything the app printed, including a credential. The module owns
  # redaction, so pipe the bounded log through its stdin gate in a one-off
  # container of the app image (this does not need the stack to be up). If the
  # filter cannot run we WITHHOLD the member - a withheld log is recoverable, a
  # leaked secret is not. A bash regex scrub is deliberately not a fallback.
  local filter_rc=0
  compose run --rm -T --no-deps --entrypoint python "$APP_SERVICE" \
    -m modulo.support.bundle --redact-stdin \
    <"$collapsed" >"$filtered" 2>"$WORK/redact-${safe}.err" || filter_rc=$?
  if (( filter_rc != 0 )); then
    local reason
    reason="$(sed -n '1,3p' "$WORK/redact-${safe}.err" | tr '\n' ' ')"
    note "$rel" "log tail withheld: redaction via 'python -m modulo.support.bundle --redact-stdin' in a one-off '$APP_SERVICE' container was unavailable (exit $filter_rc): ${reason:-no output}"
    LOGS_WITHHELD=$((LOGS_WITHHELD + 1))
    rm -f "$filtered"
    return 0
  fi

  mkdir -p "$(dirname "$dest")"
  mv "$filtered" "$dest"
  register_member "$rel" "last $TAIL_LINES lines for service '$service', repeated lines collapsed, redacted" "docker" "module (modulo.support.bundle --redact-stdin)"
}

# ---------------------------------------------------------------------------
# Docker-level collection - runs even when the stack is down.
# ---------------------------------------------------------------------------
progress "collecting docker-level facts"

collect_member "docker/compose-ps.txt" \
  "docker compose ps -a: state, health, restart counts, exit codes" \
  "$SOURCE_DOCKER" "$REDACTION_DOCKER_OUTPUT" \
  -- compose ps -a

collect_member "docker/daemon.txt" \
  "docker client/server versions and selected daemon facts" \
  "$SOURCE_DOCKER" "$REDACTION_DOCKER_OUTPUT" \
  -- docker version

collect_member "docker/daemon-info.txt" \
  "selected docker daemon facts (OS, arch, driver, CPU/memory)" \
  "$SOURCE_DOCKER" "$REDACTION_DOCKER_OUTPUT" \
  -- docker info --format 'OperatingSystem={{.OperatingSystem}}{{"\n"}}OSType={{.OSType}}{{"\n"}}Architecture={{.Architecture}}{{"\n"}}ServerVersion={{.ServerVersion}}{{"\n"}}Driver={{.Driver}}{{"\n"}}NCPU={{.NCPU}}{{"\n"}}MemTotal={{.MemTotal}}'

# Compose version + compose file identity + project + resolved service list.
# NOTE: `docker compose config` (bare) is deliberately never run - it
# interpolates and prints env values. `--services` / `--volumes` print names.
collect_compose_context() {
  local rel="docker/compose-context.txt"
  local dest="$MEMBERS/$rel"
  mkdir -p "$(dirname "$dest")"

  local compose_version="unavailable"
  compose_version="$(docker compose version 2>&1 | sed -n '1p')" || compose_version="unavailable"

  local compose_sha="missing"
  if [[ -f "$COMPOSE_FILE" ]]; then
    compose_sha="$(sha256_of "$COMPOSE_FILE")"
  fi

  local services="" services_source=""
  local services_err=""
  if services="$(compose config --services 2>"$WORK/services.err")" && [[ -n "$services" ]]; then
    services_source="docker compose config --services"
  else
    services_err="$(sed -n '1,2p' "$WORK/services.err" 2>/dev/null | tr '\n' ' ')" || services_err=""
    services="$(awk '
      /^services:[[:space:]]*$/ { inside = 1; next }
      inside && /^[^[:space:]#]/ { inside = 0 }
      inside && /^  [A-Za-z0-9_.-]+:[[:space:]]*$/ {
        line = $0
        sub(/^  /, "", line)
        sub(/:.*/, "", line)
        print line
      }
    ' "$COMPOSE_FILE" 2>/dev/null)"
    services_source="derived from the compose file (docker compose config --services failed: ${services_err:-no output})"
  fi

  local project="" project_source=""
  local first_id=""
  first_id="$(compose ps -a -q 2>/dev/null | head -n 1)" || first_id=""
  if [[ -n "$first_id" ]]; then
    project="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$first_id" 2>/dev/null)" ||
      project=""
    if [[ -n "$project" ]]; then
      project_source="container label com.docker.compose.project"
    fi
  fi
  if [[ -z "$project" ]]; then
    if [[ -n "$PROJECT_NAME" ]]; then
      project="$PROJECT_NAME"
      project_source="--project-name (explicit)"
    else
      project="$(basename "$(dirname "$COMPOSE_FILE")")"
      project_source="derived from the compose file directory (compose default; no running project to verify against)"
    fi
  fi

  {
    printf 'compose_file: %s\n' "$COMPOSE_FILE"
    printf 'compose_file_sha256: %s\n' "$compose_sha"
    printf 'compose_file_contents: not collected (the file ships in the repo; the hash identifies it, and it can carry hardcoded values)\n'
    printf 'compose_version: %s\n' "$compose_version"
    printf 'project: %s\n' "$project"
    printf 'project_source: %s\n' "$project_source"
    printf 'services:\n'
    if [[ -n "$services" ]]; then
      printf '%s\n' "$services" | sed 's/^/  - /'
    else
      printf '  (none resolved)\n'
    fi
    printf 'services_source: %s\n' "$services_source"
  } >"$dest"
  register_member "$rel" "compose file paths + hashes, project name, resolved service list" "docker" "verbatim (names and hashes only)"

  COMPOSE_SERVICES="$services"
  COMPOSE_PROJECT="$project"
  COMPOSE_PROJECT_SOURCE="$project_source"
}

COMPOSE_SERVICES=""
COMPOSE_PROJECT=""
COMPOSE_PROJECT_SOURCE=""
LOGS_WITHHELD=0
collect_compose_context

# Per-container state: health, restart count and exit code are not all in the
# `ps` table, so read them from `docker inspect`.
collect_containers() {
  local rel="docker/containers.txt"
  local dest="$MEMBERS/$rel"
  mkdir -p "$(dirname "$dest")"
  local ids=""
  ids="$(compose ps -a -q 2>/dev/null)" || ids=""
  {
    printf 'name\tservice\tstate\thealth\trestarts\texit_code\timage\tsha256\n'
    local id
    for id in $ids; do
      docker inspect --format '{{.Name}}\t{{index .Config.Labels "com.docker.compose.service"}}\t{{.State.Status}}\t{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}\t{{.RestartCount}}\t{{.State.ExitCode}}\t{{.Config.Image}}\t{{.Image}}' "$id" 2>&1 ||
        printf 'inspect-failed\t-\t-\t-\t-\t-\t-\t-\n'
    done
  } >"$dest"
  if [[ -z "$ids" ]]; then
    printf '(no containers: the compose project has nothing created or running)\n' >>"$dest"
  fi
  register_member "$rel" "per-container state, health, restart count, exit code, image" "$SOURCE_DOCKER" "$REDACTION_DOCKER_OUTPUT"
}

collect_containers

# Image tags and digests per service (the `ps` table has neither).
collect_images() {
  local rel="docker/images.txt"
  local dest="$MEMBERS/$rel"
  mkdir -p "$(dirname "$dest")"
  local images=""
  images="$(compose ps -a --format '{{.Image}}' 2>/dev/null | sort -u)" || images=""
  {
    printf 'service_images (from docker compose ps -a):\n'
    if [[ -n "$images" ]]; then
      printf '%s\n' "$images" | sed 's/^/  - /'
    else
      printf '  (none: no containers)\n'
    fi
    printf '\nresolved tags + digests:\n'
    local image
    for image in $images; do
      printf '  %s\n' "$image"
      docker image inspect --format '    RepoTags={{.RepoTags}} RepoDigests={{.RepoDigests}} Id={{.Id}}' "$image" 2>&1 | sed 's/^/    /' || true
    done
    printf '\ncompose images view:\n'
    compose images 2>&1 || true
  } >"$dest"
  register_member "$rel" "image tags and digests per service" "$SOURCE_DOCKER" "$REDACTION_DOCKER_OUTPUT"
}

collect_images

# Volume NAMES only - never contents, never a mount inspection.
collect_volumes() {
  local rel="docker/volumes.txt"
  local dest="$MEMBERS/$rel"
  mkdir -p "$(dirname "$dest")"
  local declared="" rc=0
  declared="$(compose config --volumes 2>&1)" || rc=$?
  {
    printf 'declared by the compose file (names only):\n'
    if (( rc == 0 )); then
      if [[ -n "$declared" ]]; then
        printf '%s\n' "$declared" | sed 's/^/  - /'
      else
        printf '  (none declared)\n'
      fi
    else
      printf '  (docker compose config --volumes failed, exit %d - its error text follows verbatim)\n' "$rc"
      printf '%s\n' "$declared"
    fi
    printf '\ncreated for the compose project (names only):\n'
    if [[ -n "$COMPOSE_PROJECT" ]]; then
      docker volume ls --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" --format '  - {{.Name}}' 2>&1 ||
        printf '  (docker volume ls failed)\n'
    else
      printf '  (project unknown; volume list skipped)\n'
    fi
    printf '\nvolume contents: not collected (names only by design)\n'
  } >"$dest"
  if (( rc != 0 )); then
    note "$rel" "docker compose config --volumes failed (exit $rc); its error text is in the member"
  fi
  register_member "$rel" "volume names only (declared + created); no contents" "$SOURCE_DOCKER" "verbatim (names only)"
}

collect_volumes

# Logs per resolved service.
if [[ -n "$COMPOSE_SERVICES" ]]; then
  while IFS= read -r service; do
    [[ -n "$service" ]] || continue
    collect_logs "$service"
  done <<<"$COMPOSE_SERVICES"
else
  note "docker/logs/" "no services were resolved, so no logs could be collected"
fi

# ---------------------------------------------------------------------------
# Host facts + clock
# ---------------------------------------------------------------------------
progress "collecting host facts"

collect_host_facts() {
  local rel="host/facts.txt"
  local dest="$MEMBERS/$rel"
  mkdir -p "$(dirname "$dest")"
  {
    printf 'uname: %s\n' "$(uname -a 2>&1)"
    printf 'arch: %s\n' "$(uname -m 2>&1)"
    if [[ -r /etc/os-release ]]; then
      grep -E '^(PRETTY_NAME|NAME|VERSION_ID|ID)=' /etc/os-release | sed 's/^/os-release: /' || true
    fi
    if command -v sw_vers >/dev/null 2>&1; then
      sw_vers 2>&1 | sed 's/^/sw_vers: /' || true
    fi
    printf 'docker_client: %s\n' "$(docker version --format '{{.Client.Version}}' 2>&1)"
    printf 'docker_server: %s\n' "$(docker version --format '{{.Server.Version}}' 2>&1)"
    printf 'compose: %s\n' "$(docker compose version 2>&1 | sed -n '1p')"
  } >"$dest"
  register_member "$rel" "OS, kernel, arch, docker client/server, compose version" "host" "verbatim"
}

collect_host_facts

collect_clock() {
  local rel="host/clock.txt"
  local dest="$MEMBERS/$rel"
  mkdir -p "$(dirname "$dest")"
  {
    printf 'utc_now: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'epoch_seconds: %s\n' "$(date -u +%s)"
    printf 'utc_offset: %s\n' "$(date +%z)"
    if command -v timedatectl >/dev/null 2>&1; then
      printf 'timedatectl:\n'
      timedatectl status 2>&1 | sed -n '1,12p' | sed 's/^/  /' || true
    else
      printf 'timedatectl: not available (no local NTP sync state; no network time probe is made)\n'
    fi
  } >"$dest"

  # Container-vs-host drift. Containers share the host clock, so a non-zero
  # value here means the wall clock moved between the two samples (or the
  # container has its own clock source) - it is recorded, not judged.
  local cid=""
  cid="$(compose ps -q "$APP_SERVICE" 2>/dev/null | head -n 1)" || cid=""
  if [[ -n "$cid" ]] && [[ "$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null)" == "true" ]]; then
    local host_epoch container_epoch
    host_epoch="$(date -u +%s)"
    container_epoch="$(compose exec -T "$APP_SERVICE" date -u +%s 2>/dev/null | tr -d '\r\n')" || container_epoch=""
    if [[ "$container_epoch" =~ ^[0-9]+$ ]]; then
      printf 'container_clock_minus_host_seconds: %s\n' "$(( container_epoch - host_epoch ))" >>"$dest"
      printf 'container_clock_note: containers share the host clock; a non-zero value means the wall clock moved between samples\n' >>"$dest"
    else
      printf 'container_clock_minus_host_seconds: unavailable (could not read the container clock)\n' >>"$dest"
    fi
  else
    printf 'container_clock_minus_host_seconds: unavailable (no running %s container)\n' "$APP_SERVICE" >>"$dest"
  fi
  register_member "$rel" "host UTC time, offset, NTP sync state, container-vs-host drift" "host" "verbatim"
}

collect_clock

# ---------------------------------------------------------------------------
# App-level collection - only when the app container is reachable. The CLI is
# invoked inside the container; the HTTP API is never used.
# ---------------------------------------------------------------------------
FORMAT_VERSION="$DEFAULT_FORMAT_VERSION"
APP_COLLECTED=0
REF=""
APP_REF_LINE=""

json_field() {
  # json_field <json> <key> -> value or empty. python3 when available (the JSON
  # may be pretty-printed); a tolerant sed fallback otherwise.
  local json="$1" key="$2"
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$json" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
value = data.get(sys.argv[1])
if isinstance(value, (dict, list)):
    print(json.dumps(value))
elif value is not None:
    print(value)
' "$key" 2>/dev/null || true
  else
    printf '%s' "$json" | tr -d '\n' |
      sed -n 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
  fi
}

collect_app() {
  if (( COLLECT_APP == 0 )); then
    record_missing "app" "--no-app was given: the in-container app bundle was not requested"
    return 0
  fi

  local cid=""
  cid="$(compose ps -q "$APP_SERVICE" 2>/dev/null | head -n 1)" || cid=""
  if [[ -z "$cid" ]]; then
    record_missing "app" "no '$APP_SERVICE' container exists for this compose project - the stack is down or the service never started"
    return 0
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null)" != "true" ]]; then
    record_missing "app" "the '$APP_SERVICE' container exists but is not running - app-level members cannot be collected from a stopped container"
    return 0
  fi

  progress "collecting app-level facts from the '$APP_SERVICE' container"

  local remote_path="/tmp/modulo-support.zip"
  local json_out=""
  local rc=0
  json_out="$(compose exec -T "$APP_SERVICE" python -m modulo.support.bundle --out "$remote_path" --json 2>"$WORK/app-exec.err")" || rc=$?
  if (( rc != 0 )); then
    local reason
    reason="$(sed -n '1,3p' "$WORK/app-exec.err" | tr '\n' ' ')"
    record_missing "app" "python -m modulo.support.bundle failed in the '$APP_SERVICE' container (exit $rc): ${reason:-no output}"
    return 0
  fi

  local app_zip="$WORK/app.zip"
  rc=0
  docker cp "$cid:$remote_path" "$app_zip" >"$WORK/app-cp.log" 2>&1 || rc=$?
  if (( rc != 0 )); then
    local reason
    reason="$(sed -n '1,3p' "$WORK/app-cp.log" | tr '\n' ' ')"
    record_missing "app" "docker cp of $remote_path failed (exit $rc): ${reason:-no output}"
    return 0
  fi
  if [[ ! -s "$app_zip" ]]; then
    record_missing "app" "docker cp produced an empty file for $remote_path"
    return 0
  fi

  # The module owns the ref line and the format version.
  local module_ref module_ref_line module_version
  module_ref="$(json_field "$json_out" ref)"
  module_ref_line="$(json_field "$json_out" ref_line)"
  module_version="$(json_field "$json_out" bundle_format_version)"
  if [[ "$module_version" =~ ^[0-9]+$ ]]; then
    FORMAT_VERSION="$module_version"
  fi
  if [[ -n "$module_ref_line" ]]; then
    APP_REF_LINE="$module_ref_line"
  elif [[ -n "$module_ref" ]]; then
    APP_REF_LINE="modulo support bundle $FORMAT_VERSION ref=$module_ref"
  fi
  REF="${module_ref:-}"

  # Keep the module's own CLI answer in the bundle: it is the machine-readable
  # record of what the app produced.
  mkdir -p "$MEMBERS/app"
  printf '%s\n' "$json_out" >"$MEMBERS/app/bundle-cli.json"
  register_member "app/bundle-cli.json" "the in-container CLI's --json answer (ref + path + format version)" "$SOURCE_APP" "$REDACTION_APP_MODULE"

  if ! command -v unzip >/dev/null 2>&1; then
    cp "$app_zip" "$MEMBERS/app/modulo-support.zip"
    register_member "app/modulo-support.zip" "app bundle produced by modulo.support.bundle, nested because unzip is not installed" "$SOURCE_APP" "$REDACTION_APP_MODULE"
    note "app/modulo-support.zip" "unzip is not installed; the app bundle is nested instead of merged flat"
    APP_COLLECTED=1
    return 0
  fi

  # Defensive: refuse to merge a member path that could escape the staging dir.
  local unsafe=""
  unsafe="$(unzip -Z1 "$app_zip" | grep -E '(^/|(^|/)\.\.(/|$))' || true)"
  if [[ -n "$unsafe" ]]; then
    record_missing "app" "the app bundle contains unsafe member paths and was not merged: $(printf '%s' "$unsafe" | head -n 3 | tr '\n' ' ')"
    return 0
  fi

  local extract="$WORK/app-extract"
  mkdir -p "$extract"
  rc=0
  unzip -q -o "$app_zip" -d "$extract" >"$WORK/app-unzip.log" 2>&1 || rc=$?
  if (( rc != 0 )); then
    record_missing "app" "unzip of the app bundle failed (exit $rc): $(sed -n '1,2p' "$WORK/app-unzip.log" | tr '\n' ' ')"
    return 0
  fi

  # Merge flat. The module's own MANIFEST.txt / report.json are preserved
  # under app/ - the composite root ones supersede them, and nothing is lost.
  local rel
  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    case "$rel" in
      MANIFEST.txt)
        mv "$extract/$rel" "$MEMBERS/app/MANIFEST.txt"
        register_member "app/MANIFEST.txt" "the app bundle's own manifest (superseded by the composite root MANIFEST.txt)" "$SOURCE_APP" "$REDACTION_APP_MODULE"
        ;;
      report.json)
        mv "$extract/$rel" "$MEMBERS/app/report.json"
        register_member "app/report.json" "the app bundle's own inventory + per-layer redaction counts" "$SOURCE_APP" "$REDACTION_APP_MODULE"
        ;;
      *)
        mkdir -p "$(dirname "$MEMBERS/$rel")"
        mv "$extract/$rel" "$MEMBERS/$rel"
        register_member "$rel" "app-level member produced by modulo.support.bundle" "$SOURCE_APP" "$REDACTION_APP_MODULE"
        ;;
    esac
  done < <(cd "$extract" && find . -type f -print | sed 's|^\./||')

  APP_COLLECTED=1
  return 0
}

collect_app

# ---------------------------------------------------------------------------
# Composite MANIFEST.txt + report.json
# ---------------------------------------------------------------------------
progress "writing manifest"

if [[ -z "$REF" ]]; then
  REF="MSB-$FORMAT_VERSION-$STAMP-$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || true)"
fi
if [[ -z "$APP_REF_LINE" ]]; then
  APP_REF_LINE="modulo support bundle $FORMAT_VERSION ref=$REF"
fi
REF_LINE="$APP_REF_LINE"

missing_json_list="$(cut -f1 "$MISSING_FILE" | sed 's/.*/"&"/' | paste -sd, -)" || missing_json_list=""

MANIFEST="$MEMBERS/MANIFEST.txt"
{
  printf 'Modulo support bundle\n'
  printf '=====================\n'
  printf 'bundle_format_version: %s\n' "$FORMAT_VERSION"
  printf 'ref: %s\n' "$REF"
  printf 'ref_line: %s\n' "$REF_LINE"
  printf 'generated_at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'generated_by: scripts/support-bundle.sh (Docker Compose collector)\n'
  printf 'deployment: docker-compose\n'
  printf 'compose_file: %s\n' "$COMPOSE_FILE"
  printf 'project: %s\n' "${COMPOSE_PROJECT:-unknown}"
  printf 'missing: [%s]\n' "$missing_json_list"
  if [[ -s "$MISSING_FILE" ]]; then
    printf '\nmissing sources (recorded, never silently omitted):\n'
    while IFS=$'\t' read -r src reason; do
      [[ -n "$src" ]] || continue
      printf '  %s: %s\n' "$src" "$reason"
    done <"$MISSING_FILE"
  fi
  printf '\nwhat each member carries:\n'
  total_bytes=0
  member_count=0
  while IFS=$'\t' read -r rel desc source redaction; do
    [[ -n "$rel" ]] || continue
    bytes="$(file_size "$MEMBERS/$rel")"
    total_bytes=$((total_bytes + bytes))
    member_count=$((member_count + 1))
    printf '  %-46s %10s  [%s] %s (%s)\n' "$rel" "$(human_size "$bytes")" "$source" "$desc" "$redaction"
  done < <(sort "$REGISTRY")
  printf '\nmembers: %s (%s uncompressed)\n' "$member_count" "$(human_size "$total_bytes")"
  if [[ -s "$NOTES_FILE" ]]; then
    printf '\nnotes:\n'
    while IFS=$'\t' read -r rel reason; do
      [[ -n "$rel" ]] || continue
      printf '  %s: %s\n' "$rel" "$reason"
    done <"$NOTES_FILE"
  fi
  printf '\nredaction: app-level members and every docker-level log tail are redacted by\n'
  printf 'modulo.support.bundle. A log tail whose redaction filter could not run is\n'
  printf 'withheld and listed above, never shipped raw. Open this bundle and check its\n'
  printf 'contents before sending it to anyone.\n'
} >"$MANIFEST"

REPORT="$MEMBERS/report.json"
{
  printf '{\n'
  printf '  "bundle_format_version": %s,\n' "$FORMAT_VERSION"
  printf '  "ref": "%s",\n' "$(json_escape "$REF")"
  printf '  "ref_line": "%s",\n' "$(json_escape "$REF_LINE")"
  printf '  "generated_by": "scripts/support-bundle.sh",\n'
  printf '  "generated_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '  "deployment": {\n'
  printf '    "kind": "docker-compose",\n'
  printf '    "compose_files": ["%s"],\n' "$(json_escape "$COMPOSE_FILE")"
  printf '    "compose_file_sha256": "%s",\n' "$(json_escape "$(sha256_of "$COMPOSE_FILE" 2>/dev/null || printf 'missing')")"
  printf '    "project": "%s",\n' "$(json_escape "${COMPOSE_PROJECT:-}")"
  printf '    "project_source": "%s",\n' "$(json_escape "${COMPOSE_PROJECT_SOURCE:-}")"
  printf '    "services": ['
  first=1
  while IFS= read -r service; do
    [[ -n "$service" ]] || continue
    if (( first == 1 )); then first=0; else printf ', '; fi
    printf '"%s"' "$(json_escape "$service")"
  done <<<"$COMPOSE_SERVICES"
  printf ']\n'
  printf '  },\n'
  printf '  "app_bundle": {"collected": %s' "$([[ "$APP_COLLECTED" -eq 1 ]] && printf 'true' || printf 'false')"
  if [[ "$APP_COLLECTED" -eq 1 ]] && [[ -f "$MEMBERS/app/report.json" ]]; then
    printf ', "report_member": "app/report.json"'
  fi
  printf '},\n'
  printf '  "members": [\n'
  first=1
  while IFS=$'\t' read -r rel desc source redaction; do
    [[ -n "$rel" ]] || continue
    if (( first == 1 )); then first=0; else printf ',\n'; fi
    printf '    {"name": "%s", "bytes": %s, "source": "%s", "redaction": "%s"}' \
      "$(json_escape "$rel")" "$(file_size "$MEMBERS/$rel")" "$(json_escape "$source")" "$(json_escape "$redaction")"
  done < <(sort "$REGISTRY")
  printf '\n  ],\n'
  printf '  "missing": ['
  first=1
  while IFS=$'\t' read -r src reason; do
    [[ -n "$src" ]] || continue
    if (( first == 1 )); then first=0; else printf ', '; fi
    printf '{"source": "%s", "reason": "%s"}' "$(json_escape "$src")" "$(json_escape "$reason")"
  done <"$MISSING_FILE"
  printf '],\n'
  printf '  "notes": ['
  first=1
  while IFS=$'\t' read -r rel reason; do
    [[ -n "$rel" ]] || continue
    if (( first == 1 )); then first=0; else printf ', '; fi
    printf '{"member": "%s", "note": "%s"}' "$(json_escape "$rel")" "$(json_escape "$reason")"
  done <"$NOTES_FILE"
  printf ']\n'
  printf '}\n'
} >"$REPORT"

# ---------------------------------------------------------------------------
# Archive: write to a temp name inside the destination directory, verify, then
# move into place. A failure never leaves something that looks like a bundle.
# ---------------------------------------------------------------------------
progress "writing bundle"

OUT_DIR="$(dirname "$OUT")"
mkdir -p "$OUT_DIR"
TMP_ZIP="$OUT_DIR/.$(basename "$OUT").partial.$$"
rm -f "$TMP_ZIP"

build_zip() {
  (
    cd "$MEMBERS"
    find . -type f -print | sed 's|^\./||' | zip -q -@ "$TMP_ZIP"
  )
}

build_zip || die "zip failed to build the bundle archive"

zip_size="$(file_size "$TMP_ZIP")"
if (( zip_size > SIZE_CAP_BYTES )); then
  # Format requirement: stay under the 5 MB cap by tightening the log members
  # further, and record that in the manifest.
  progress "bundle exceeds the 5 MB cap ($(human_size "$zip_size")); tightening log members"
  for log_member in "$MEMBERS"/docker/logs/*.log.tail; do
    [[ -f "$log_member" ]] || continue
    head -c 65536 "$log_member" >"$log_member.tight" || true
    printf '\n[collector] truncated to 64 KiB to keep the bundle under the 5 MB cap\n' >>"$log_member.tight"
    mv "$log_member.tight" "$log_member"
  done
  printf 'size_cap: log members were truncated to 64 KiB each to keep the compressed bundle under 5 MB\n' >>"$MANIFEST"
  rm -f "$TMP_ZIP"
  build_zip || die "zip failed to rebuild the bundle archive"
  zip_size="$(file_size "$TMP_ZIP")"
  if (( zip_size > SIZE_CAP_BYTES )); then
    note "bundle" "the compressed bundle is still over the 5 MB cap after log truncation"
  fi
fi

mv "$TMP_ZIP" "$OUT"
TMP_ZIP=""

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
printf '%s\n' "$REF_LINE"
printf 'bundle: %s (%s)\n' "$OUT" "$(human_size "$zip_size")"
if (( LOGS_WITHHELD > 0 )); then
  printf 'Log tails were withheld because redaction via modulo.support.bundle was unavailable (%d service(s)).\n' "$LOGS_WITHHELD"
fi
printf 'Open the bundle and check its contents before sending it to anyone.\n'
