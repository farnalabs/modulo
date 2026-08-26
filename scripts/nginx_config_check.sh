#!/usr/bin/env bash
# Validate the nginx server configs (deploy/nginx/*.conf, deploy/fly/nginx.conf)
# with `nginx -t`. Guards against silent CSP/header regressions on config-only
# changes (FAR-447 review Minor-3).
#
# Each server config `include`s the shared edge CSP snippet
# (deploy/nginx/security_headers.conf), which is rendered at boot by
# render_csp.sh. We render the DEFAULT snippet here (no MODULO_MONITOR_DOMAINS)
# and validate syntax. The frontend-only conf (deploy/nginx/default.conf)
# proxies to the docker-compose service host `backend`; for a syntax-only check
# we point it at loopback (matching the all-in-one / Fly confs, which use
# 127.0.0.1).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NGINX_DIR="$REPO_ROOT/deploy/nginx"
FLY_CONF="$REPO_ROOT/deploy/fly/nginx.conf"

# Render the default snippet (mirrors render_csp.sh defaults).
export CONNECT_SRC="*.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com ws: wss:"
python3 - "$NGINX_DIR/security_headers.conf.in" > /tmp/security_headers.conf <<'PY'
import os, sys
tpl = open(sys.argv[1]).read()
sys.stdout.write(tpl.replace("${CONNECT_SRC}", os.environ["CONNECT_SRC"]))
PY

for conf in "$NGINX_DIR/all-in-one.conf" "$NGINX_DIR/default.conf" "$FLY_CONF"; do
  name="$(basename "$conf")"
  tmp_conf="/tmp/$name"
  cp "$conf" "$tmp_conf"
  if [ "$name" = "default.conf" ]; then
    sed -i 's#http://backend:8000#http://127.0.0.1:8000#g' "$tmp_conf"
  fi
  cat > /tmp/nginx-test.conf <<EOF
events {}
pid /run/nginx-test.pid;
http {
  include /tmp/$name;
}
EOF
  echo "=== $conf ==="
  docker run --rm \
    -v "/tmp/nginx-test.conf:/etc/nginx/nginx.conf:ro" \
    -v "$tmp_conf:/tmp/$name:ro" \
    -v "/tmp/security_headers.conf:/usr/share/nginx/html/security_headers.conf:ro" \
    nginx:alpine nginx -t -c /etc/nginx/nginx.conf
done
