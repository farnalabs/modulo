# Web machine recovery (app-modulo)

Runbook for the 2026-09-02 incident class: the production web machine of
`app-modulo` (process group `app`) stays alive (state `started`) but wedged -
its HTTP server on port 8080 stops answering, and the Fly service check
`servicecheck-00-http-8080` goes critical and stays critical until the machine
is restarted.

## Symptoms

- Requests to app.modulo.run time out.
- `flyctl checks list -a app-modulo` shows `servicecheck-00-http-8080`
  critical with output like "context deadline exceeded".
- Fly proxy logs show "could not find a good candidate within 40 attempts at
  load balancing".
- `flyctl machines list -a app-modulo` still shows the `app` process-group
  machine in state `started`.

The machine being `started` is the key discriminator: this is a live-but-wedged
machine, NOT a stopped-machine outage. `fly machines start` is the wrong tool
here - the machine needs a restart.

## Diagnose (read-only)

```sh
# Service-check status: expect servicecheck-00-http-8080 critical.
flyctl checks list -a app-modulo

# Machine states and process groups: the app machine should be `started`.
flyctl machines list -a app-modulo

# Recent logs: worker machines keep logging actively while the app machine's
# service check fails - that split (workers alive, web check critical) is the
# signature of a wedge rather than a crash.
flyctl logs -a app-modulo --no-tail
```

## Recover

1. Identify the machine in the `app` process group:

   ```sh
   flyctl machines list -a app-modulo
   ```

   Note the ID from the row whose PROCESS GROUP column is `app`.

2. Restart it (brief downtime; see Notes below):

   ```sh
   flyctl machine restart <machine-id> -a app-modulo
   ```

3. Verify recovery:

   ```sh
   # Service check back to passing:
   flyctl checks list -a app-modulo

   # Site returns HTTP 200:
   curl -sS -o /dev/null -w "%{http_code}\n" https://app.modulo.run
   ```

## Notes

- Restarting the `app` machine is safe for running pipelines: pipeline runs
  (SAQ) execute on the separate worker-process machines, which the restart
  does not touch.
- When this procedure is needed, the web server is by definition already
  unresponsive, so in-flight HTTP requests are already dead; the restart
  cannot make that worse.
- There is no automated restart: the ops watchdogs were removed in FAR-283
  (commit 33652cc7a). The remaining `.github/workflows/uptime-monitor.yml`
  only probes `/healthz/ready` and files a Linear ticket when the check stays
  critical - it never restarts the machine. This runbook is therefore the only
  recovery path when the app machine becomes unresponsive.
- VM sizing in `fly.toml` (`[[vm]]`) applies to NEW machines only. Resizing
  the LIVE machine takes an explicit update (brief restart):

  ```sh
  flyctl machine update <machine-id> -a app-modulo --vm-size shared-cpu-4x
  ```

  Find the app-group machine id via `flyctl machines list -a app-modulo`.
  This matches the machine-budget comment block in `fly.toml`.
