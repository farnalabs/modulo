#!/usr/bin/env bash
# Validate the nginx server configs (deploy/nginx/*.conf, deploy/fly/nginx.conf)
# with `nginx -t`, and validate the shared edge CSP snippet by running the REAL
# renderer. Guards against silent CSP/header regressions on config-only changes
# (FAR-447 review Minor-3).
#
# Each server config `include`s the shared edge CSP snippet
# (deploy/nginx/security_headers.conf), which is rendered at boot by
# render_csp.sh. This script invokes deploy/nginx/render_csp.sh itself (inside
# nginx:alpine, which has both envsubst and nginx) rather than re-implementing
# the render, so the actual producer -- including its sanitisation -- is what
# gets tested (FAR-447 re-review MAJOR: prove-the-fix).
#
# Cases covered:
#   default  unset env renders the committed default snippet (drift check)
#   legit    MODULO_MONITOR_DOMAINS / VITE_GRAFANA_FARO_URL origins land in
#            connect-src
#   hostile  a value containing ; CR LF " $ \ and a bare * is neutralised and
#            still renders a config that `nginx -t` accepts
#
# The frontend-only conf (deploy/nginx/default.conf) proxies to the
# docker-compose service host `backend`; for a syntax-only check we point it at
# loopback (matching the all-in-one / Fly confs, which use 127.0.0.1).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NGINX_DIR="$REPO_ROOT/deploy/nginx"
FLY_CONF="$REPO_ROOT/deploy/fly/nginx.conf"
IMAGE="nginx:alpine"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# Rendered snippets and copied server configs live in separate directories:
# deploy/nginx/default.conf would otherwise clobber the default.conf render.
RENDERS="$WORK/renders"
CONFS="$WORK/confs"
mkdir -p "$RENDERS" "$CONFS"

DEFAULT_CONNECT_SRC="*.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com ws: wss:"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# Container-side driver: renders the three fixtures with the real render_csp.sh.
# Hostile values are built here (not passed through `docker -e`) so embedded CR
# and LF survive verbatim.
cat > "$WORK/render_cases.sh" <<'CASES'
#!/bin/sh
set -eu
command -v envsubst >/dev/null 2>&1 || apk add --no-cache gettext >/dev/null 2>&1
TEMPLATE=/work/deploy/nginx/security_headers.conf.in
export TEMPLATE

env -u MODULO_MONITOR_DOMAINS -u VITE_GRAFANA_FARO_URL \
    OUT=/out/default.conf sh /work/deploy/nginx/render_csp.sh

MODULO_MONITOR_DOMAINS='*.honeycomb.io otel.example.com:4318' \
VITE_GRAFANA_FARO_URL='https://faro.example.com/collect/abc' \
    OUT=/out/legit.conf sh /work/deploy/nginx/render_csp.sh

# ; and CR/LF break out of the directive; " terminates the add_header string;
# $undef is an nginx variable reference; \ escapes; a bare * opens connect-src
# to any host. Single quotes keep the backslash literal; only the CR/LF pair
# goes through printf.
crlf="$(printf 'crlf.example.com\r\nlf.example.com')"
hostile='evil.com; script-src * bad"quote $undef back\slash ev*l.com '"$crlf"
MODULO_MONITOR_DOMAINS="$hostile" \
VITE_GRAFANA_FARO_URL='https://faro.evil.com";add_header X-Pwned "1/collect' \
    OUT=/out/hostile.conf sh /work/deploy/nginx/render_csp.sh
CASES

echo "=== rendering CSP snippet fixtures via deploy/nginx/render_csp.sh ==="
docker run --rm \
  -v "$REPO_ROOT:/work:ro" \
  -v "$RENDERS:/out" \
  -v "$WORK/render_cases.sh:/render_cases.sh:ro" \
  "$IMAGE" sh /render_cases.sh

for f in default legit hostile; do
  [ -s "$RENDERS/$f.conf" ] || fail "render_csp.sh produced no output for the '$f' case"
done

# --- default render must equal the committed snippet (template/.conf drift) ---
if ! diff -u "$NGINX_DIR/security_headers.conf" "$RENDERS/default.conf"; then
  fail "deploy/nginx/security_headers.conf is stale: it does not match the default
render of security_headers.conf.in. Re-render it with:
  TEMPLATE=deploy/nginx/security_headers.conf.in \\
  OUT=deploy/nginx/security_headers.conf sh deploy/nginx/render_csp.sh"
fi
grep -qF "connect-src 'self' $DEFAULT_CONNECT_SRC;" "$RENDERS/default.conf" \
  || fail "default connect-src drifted from the expected allowlist"
echo "OK: committed security_headers.conf matches the default render"

# --- configured telemetry origins must actually be allowed ---
for origin in '*.honeycomb.io' 'otel.example.com:4318' 'faro.example.com'; do
  grep -qF "$origin" "$RENDERS/legit.conf" \
    || fail "configured origin '$origin' missing from the rendered connect-src"
done
echo "OK: MODULO_MONITOR_DOMAINS / VITE_GRAFANA_FARO_URL origins reach connect-src"

# --- hostile input must be neutralised ---
# A CR/LF, ';' or '"' that survived would add or split directives, so the
# hostile render must have exactly the same shape as the default one: same line
# count, same number of add_header directives. Leftover inert hostname-shaped
# tokens (e.g. "script-src") are acceptable -- they are simply CSP sources that
# never match -- but no metacharacter and no wildcard-everything may survive.
if [ "$(wc -l < "$RENDERS/hostile.conf")" -ne "$(wc -l < "$RENDERS/default.conf")" ]; then
  fail "hostile value changed the line count: it broke out of the directive"
fi
if [ "$(grep -c 'add_header' "$RENDERS/hostile.conf")" -ne 6 ]; then
  fail "hostile value injected or removed an add_header directive"
fi
csp_line="$(grep -F 'add_header Content-Security-Policy' "$RENDERS/hostile.conf")"
for bad in '"quote' '$undef' 'ev*l.com' 'evil.com' 'faro.evil.com'; do
  if printf '%s' "$csp_line" | grep -qF -- "$bad"; then
    fail "hostile token '$bad' survived sanitisation"
  fi
done
if printf '%s' "$csp_line" | grep -q '\\'; then
  fail 'hostile value kept a backslash (nginx escape character)'
fi
connect_src="$(printf '%s' "$csp_line" | sed -n "s/.*connect-src \([^;]*\);.*/\1/p")"
[ -n "$connect_src" ] || fail "could not extract connect-src from the hostile render"
if printf '%s' " $connect_src " | grep -qF ' * '; then
  fail "hostile value widened connect-src to '*'"
fi
echo "OK: hostile MODULO_MONITOR_DOMAINS / VITE_GRAFANA_FARO_URL neutralised"

# --- nginx -t every server config, against the default and hostile renders ---
for snippet in default hostile; do
  for conf in "$NGINX_DIR/all-in-one.conf" "$NGINX_DIR/default.conf" "$FLY_CONF"; do
    name="$(basename "$conf")"
    tmp_conf="$CONFS/$name"
    cp "$conf" "$tmp_conf"
    if [ "$name" = "default.conf" ]; then
      sed -i 's#http://backend:8000#http://127.0.0.1:8000#g' "$tmp_conf"
    fi
    cat > "$WORK/nginx-test.conf" <<EOF
events {}
pid /run/nginx-test.pid;
http {
  include /tmp/$name;
}
EOF
    echo "=== $conf ($snippet snippet) ==="
    docker run --rm \
      -v "$WORK/nginx-test.conf:/etc/nginx/nginx.conf:ro" \
      -v "$tmp_conf:/tmp/$name:ro" \
      -v "$RENDERS/$snippet.conf:/etc/nginx/security_headers.conf:ro" \
      "$IMAGE" nginx -t -c /etc/nginx/nginx.conf
  done
done
