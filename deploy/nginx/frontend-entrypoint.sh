#!/bin/sh
# Frontend (SPA-only) nginx image entrypoint: render the shared edge CSP
# snippet (honouring MODULO_MONITOR_DOMAINS / VITE_GRAFANA_FARO_URL) and then
# exec nginx. FAR-447 review Major-1.
set -e
/usr/local/bin/render_csp.sh
exec nginx -g "daemon off;"
