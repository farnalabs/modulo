#!/bin/sh
set -e

echo "Running database migrations..."
alembic upgrade heads

echo "Starting supervisord..."
exec supervisord -c /etc/supervisor/conf.d/supervisord.conf
