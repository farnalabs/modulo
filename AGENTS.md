# Modulo

Product-specific agent guidance for the `farnalabs/modulo` repository.

## Lessons Learned

### Failed deploys must self-heal and outages must be detected (2026-08-23)
A single failed prod deploy (Fly control-plane blip during health-check polling, NOT a code regression) left app.modulo.run down for ~7h with no alert. Hardening (see FAR-400): `fly deploy` in `.github/workflows/deploy.yml` MUST use `--auto-rollback`; add a post-deploy `/healthz/ready` gate that runs `fly releases rollback` if the app isn't serving; and a scheduled `.github/workflows/uptime-monitor.yml` MUST probe `app.modulo.run/healthz/ready` every 10 min and alert (fail the run + open a Linear ticket) on outage. Never rely on a failed deploy to merely fail the job — it must revert to the last good release so prod stays up.

## Known Issues
