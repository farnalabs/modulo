# deploy/watchdog — health watchdog (Gatus)

Config-as-code for the `watchdog` service in the root `docker-compose.yml`:
a [Gatus](https://gatus.io) instance that probes Modulo's readiness and tells
the operator when the deployment is unhealthy — including when it is fully
down, which is the one failure the app cannot report about itself.

Operator-facing documentation lives in [`docs/deployment.md` §Health watchdog](../../docs/deployment.md#health-watchdog-docker-compose);
this directory is the implementation.

| File | Role |
|---|---|
| `config.yaml` | The Gatus config: the endpoint, its conditions, and the email alerting block. Mounted read-only into the container at `/config/config.yaml`. |
| `entrypoint.sh` | Prints one startup line — alerting on, or off and exactly which variables are missing — then `exec`s `/gatus`. |
| `Dockerfile` | Re-hosts the pinned upstream `/gatus` binary on `alpine:3.20`. |

## Why a derived image

Upstream `twinproduction/gatus:v5.37.0` is built `FROM scratch`: no shell and
no HTTP client. Verified before this directory was written:

```console
$ docker run --rm --entrypoint /bin/sh twinproduction/gatus:v5.37.0 -c 'echo hi'
exec: "/bin/sh": stat /bin/sh: no such file or directory   # exit 127
```

That rules out both an in-container healthcheck and any startup line of our
own, so the image keeps upstream's static `/gatus` binary and adds what Alpine
already provides: `/bin/busybox` (used by `entrypoint.sh` and by the compose
healthcheck's exec-form `/bin/busybox wget ...`), the busybox `wget` applet,
and `/etc/ssl/certs/ca-certificates.crt` for SMTP STARTTLS. No `apk add`, so
there is no network dependency at build time beyond the two base images.

Both bases are pinned (no `latest`/`stable`): `twinproduction/gatus:v5.37.0`
(tag verified on the Docker Hub tags API, published 2026-09-24) and
`alpine:3.20`.

The image runs as an unprivileged `gatus` user: the binary is static, the
dashboard binds the unprivileged port 8080, the config is read-only, and no
`storage` backend is configured, so root is never needed.

## Quiet degradation without credentials

The compose deployment ships no SMTP configuration by default, so the default
state is *monitoring, not alerting*. It must not crash, exit, or block the
stack — and it must not be silent either.

The mechanism chosen is **no config rendering at all**: Gatus expands `${VAR}`
in `config.yaml` itself, and — verified empirically against v5.37.0 with six
separate container runs — it **tolerates** an email provider block whose
credentials are empty. It logs why and keeps running:

```text
[config.ValidateAlertingConfig] Ignoring provider=email due to error=from and to fields are required
[config.ValidateAlertingConfig] configuredProviders=[]; ignoredProviders=[...]
```

If an alert later fires with no provider configured, it logs and continues
rather than dying:

```text
[watchdog.handleAlertsToTrigger] Not sending alert of type=email ... because the provider wasn't configured properly
```

Because that Gatus line reads like a validation error rather than a supported
default, `entrypoint.sh` adds the operator-facing one (see table above). No
shell inside the container was assumed: `entrypoint.sh` runs under the busybox
`sh` that Alpine supplies.

Two related behaviours, both verified:

- `port: ""` (a **quoted** empty value) makes Gatus panic —
  `cannot unmarshal !!str `` into int`, exit 2. The config therefore writes
  `port:` unquoted, so an unset `SMTP_PORT` expands to YAML `null` → `0`, which
  Gatus rejects with a warning instead.
- With `EMAIL_FROM`/`ALERT_EMAIL_TO` set but no host/port, Gatus still refuses
  the provider (`port must be between 1 and 65535`) and runs normally.

## Conditions

Both conditions are load-bearing:

```yaml
- "[STATUS] == 200"
- "[BODY].status == ok"
```

`/healthz/ready` returns HTTP **200** with `"status": "degraded"` when a
non-gating sub-check is degraded, and 503 only for `"unavailable"`. Verified
against Gatus v5.37.0 serving both bodies: a 200 + `{"status":"degraded"}` body
**passes** `[STATUS] == 200` alone and **fails** with the body condition added —
i.e. the status-only check misses exactly the failure this watchdog exists to
catch.

The body value is deliberately unquoted: `[BODY].status == "ok"` compares
against the quote characters literally and never matches (also verified).

## Tests

- `backend/tests/unit/test_watchdog_config.py` — both credential states
  (alerting block wired to the right variables; config still valid with none),
  plus the compose wiring and pinning guards.
- `backend/tests/docker/test_watchdog_container.py` — the real container:
  starts with no credentials, logs the disabled line, probes, stays up.

## Not yet wired

- `deploy/compose/docker-compose.prod.yml` (production override) — follow-up.
- The Helm chart (`deploy/helm/**`) — deliberately out of scope; the owner has
  not decided whether the watchdog belongs there.
