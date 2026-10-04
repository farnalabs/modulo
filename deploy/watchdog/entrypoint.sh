#!/bin/sh
# Startup wrapper for the Modulo health watchdog (docker-compose.yml service
# `watchdog`). Runs because the derived image ships busybox - the upstream
# Gatus image is scratch-based and has no shell at all.
#
# Its whole job is one honest startup line. The container monitors the backend
# either way; this line tells the operator whether email alerts will fire and,
# when they will not, exactly which variables are missing. Gatus's own
# "Ignoring provider=email ..." message is config-validation output and does
# not say which knobs to turn - quiet must not be silent.
#
# Ends in `exec` so /gatus becomes PID 1 and receives signals directly.
set -eu

missing=""
for var in SMTP_HOST SMTP_PORT EMAIL_FROM ALERT_EMAIL_TO; do
  eval "value=\${$var:-}"
  if [ -z "$value" ]; then
    missing="$missing $var"
  fi
done

if [ -n "$missing" ]; then
  echo "watchdog: email alerting DISABLED (not set:$missing). Monitoring is ON - the Gatus dashboard and the probe keep running; set those variables to enable email alerts (see docs/deployment.md)."
else
  echo "watchdog: email alerting ENABLED (host=${SMTP_HOST} port=${SMTP_PORT} from=${EMAIL_FROM} to=${ALERT_EMAIL_TO})."
fi

exec /gatus "$@"
