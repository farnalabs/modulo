#!/bin/sh
# Render the shared edge CSP snippet (security_headers.conf) from
# security_headers.conf.in, injecting the deployment's configurable monitor
# domains into connect-src so the SPA's edge CSP honours MODULO_MONITOR_DOMAINS
# and the Grafana Faro collector (FAR-447 review Major-1). Mirrors the backend
# SecurityHeadersMiddleware in backend/src/modulo/api/middleware/security_headers.py.
#
# SECURITY: MODULO_MONITOR_DOMAINS / VITE_GRAFANA_FARO_URL are untrusted env
# input on the frontend-only nginx image (which runs no backend validator), so
# render_csp.sh must sanitise them itself. A semicolon, CR or LF breaks out of
# the connect-src directive and injects arbitrary CSP directives at the edge
# (FAR-447 re-review MAJOR — security consistency). We STRIP those chars,
# mirroring backend/src/modulo/settings.py:_validate_monitor_domains (which
# rejects them for the backend). We strip (rather than reject) so a malformed
# value degrades to "telemetry not added" instead of crashing boot on the
# backend-less image.
#
# Reads (all optional):
#   MODULO_MONITOR_DOMAINS  space-separated extra connect-src origins
#   VITE_GRAFANA_FARO_URL   Grafana Faro collector URL (its origin is allowed)
#
# Output: ${OUT} (default /etc/nginx/security_headers.conf), which each nginx
# server config `include`s. Placed OUTSIDE the document root so it is not
# publicly downloadable (FAR-447 re-review MINOR-2).
set -eu

TEMPLATE="${TEMPLATE:-/etc/nginx/security_headers.conf.in}"
OUT="${OUT:-/etc/nginx/security_headers.conf}"

# Defaults match the backend middleware's hardcoded allowlist.
CONNECT_SRC="*.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com ws: wss:"

# Strip any char that could break out of a CSP directive: ; CR LF. Mirrors the
# backend settings validator but strips instead of rejecting (see header).
sanitize() {
    printf '%s' "$1" | tr -d ';\r\n'
}

extra=""
if [ -n "${MODULO_MONITOR_DOMAINS:-}" ]; then
    extra="$extra $(sanitize "$MODULO_MONITOR_DOMAINS")"
fi
if [ -n "${VITE_GRAFANA_FARO_URL:-}" ]; then
    faro=$(printf '%s' "$VITE_GRAFANA_FARO_URL" | sed -E 's#^[a-zA-Z]+://##; s#/.*##' | tr -d ';\r\n')
    if [ -n "$faro" ]; then
        extra="$extra $faro"
    fi
fi

# Collapse any runs of spaces left by sanitised-away tokens.
extra=$(printf '%s' "$extra" | tr -s ' ')

if [ -n "$extra" ]; then
    CONNECT_SRC="$CONNECT_SRC$extra"
fi

export CONNECT_SRC
envsubst '${CONNECT_SRC}' < "$TEMPLATE" > "$OUT"
