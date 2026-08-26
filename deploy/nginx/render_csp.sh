#!/bin/sh
# Render the shared edge CSP snippet (security_headers.conf) from
# security_headers.conf.in, injecting the deployment's configurable monitor
# domains into connect-src so the SPA's edge CSP honours MODULO_MONITOR_DOMAINS
# and the Grafana Faro collector (FAR-447 review Major-1). Mirrors the backend
# SecurityHeadersMiddleware in backend/src/modulo/api/middleware/security_headers.py.
#
# Reads (all optional):
#   MODULO_MONITOR_DOMAINS  space-separated extra connect-src origins
#   VITE_GRAFANA_FARO_URL   Grafana Faro collector URL (its origin is allowed)
#
# Output: ${OUT} (default /usr/share/nginx/html/security_headers.conf), which
# each nginx server config `include`s.
set -eu

TEMPLATE="${TEMPLATE:-/etc/nginx/security_headers.conf.in}"
OUT="${OUT:-/usr/share/nginx/html/security_headers.conf}"

# Defaults match the backend middleware's hardcoded allowlist.
CONNECT_SRC="*.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com ws: wss:"

extra=""
if [ -n "${MODULO_MONITOR_DOMAINS:-}" ]; then
    extra="$extra $MODULO_MONITOR_DOMAINS"
fi
if [ -n "${VITE_GRAFANA_FARO_URL:-}" ]; then
    faro=$(printf '%s' "$VITE_GRAFANA_FARO_URL" | sed -E 's#^[a-zA-Z]+://##; s#/.*##')
    if [ -n "$faro" ]; then
        extra="$extra $faro"
    fi
fi

if [ -n "$extra" ]; then
    CONNECT_SRC="$CONNECT_SRC$extra"
fi

export CONNECT_SRC
envsubst '${CONNECT_SRC}' < "$TEMPLATE" > "$OUT"
