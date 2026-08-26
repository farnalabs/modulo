#!/bin/sh
set -e

echo "Running database migrations..."
alembic upgrade heads

echo "=== Rendering edge CSP snippet (honours MODULO_MONITOR_DOMAINS) ==="
/usr/local/bin/render_csp.sh

echo "Starting supervisord..."
exec supervisord -c /etc/supervisor/conf.d/supervisord.conf
