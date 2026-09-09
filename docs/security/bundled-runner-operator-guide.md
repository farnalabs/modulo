# Bundled Runner — operator guide

<!-- FAR-590, D4 of the Agent Execution Tiers plan (ADR 029). Audience:
     self-hosting operators enabling and running the Bundled Runner tier.
     Companion: docs/security/bundled-runner-trust-boundary.md (the threat
     model); docs/architecture.md (the Runtime Provider Hub section). -->

The Bundled Runner runs `sandbox_agent` nodes as hardened containers on YOUR
Docker engine, from a first-party pinned image, through a filtered Docker
endpoint. This guide covers enabling it, what runs where, and day-2
operations.

## 1. Prerequisites

- Docker engine reachable from the Modulo backend and SAQ workers (the same
  host in the default compose; a remote engine works identically — see
  "Remote engine" below).
- The runner image available at the pinned digest (see §3 — until the GHCR
  publish job is live, build it yourself: `deploy/docker/runner-opencode.Dockerfile`).

## 2. Enable the overlay

```bash
docker compose -f docker-compose.yml -f deploy/compose/runner.yml --profile runner up -d
```

The overlay is OFF by default (the `runner` compose profile). Enabling it:

1. Adds `docker-socket-proxy` — the filtered Docker endpoint (socket mounted
   read-only, digest-pinned image, `restart: unless-stopped`, no host ports).
2. Sets `MODULO_DOCKER_HOST=tcp://docker-socket-proxy:2375` on `backend`,
   `saq-runner`, and `saq-system` (the documented 4-step endpoint chain:
   `MODULO_DOCKER_HOST` → `DOCKER_HOST` → local socket).
3. Declares + references the dedicated `modulo-runner-workspace` bridge
   network (compose does not create unreferenced networks under `--profile`,
   so the holder service must exist — phase-0 spike finding).

**Docker endpoint env chain** (documented precedence everywhere the backend
touches Docker): constructor arg → `MODULO_DOCKER_HOST` → `DOCKER_HOST` →
local socket. The raw socket is an operator override — prefer the proxy.

**Registration matrix**: the Bundled Runner provider (`runner_docker`)
registers when any `MODULO_RUNNER_*` variable OR a Docker endpoint
(`MODULO_DOCKER_HOST`/`DOCKER_HOST`) is set. With none set, it does not
register — binding a profile to it then raises a typed dispatch error
naming `MODULO_DOCKER_HOST` (remediation copy), never a silent fallback.

## 3. The runner image + digest pinning

The shipped profile pins `modulo-runner:opencode@sha256:<digest>` (the
per-minor constant in `backend/src/modulo/db/bundled_runner_template.py`;
the compose overlay's `bundled-runner` service builds the same Dockerfile
for self-builders):

```bash
docker compose -f deploy/compose/runner.yml --profile runner build bundled-runner
```

Pin-advance process: the GHCR publish job (GA item) bumps `OPENCODE_VERSION`
(current minor + N-1 support window), scans (trivy/grype fail high/critical
with a documented time-boxed CVE-allowlist), runs container-structure-test,
publishes `released-<minor>`, and advances the seed/docs digest constant
(the digest-drift guard asserts the constant matches the published tag).
Until that job is live, an operator advance is manual: build, scan, pin the
new digest in the template constants, and re-seed/apply.

Operator-pinned older digests SURVIVE: seeding is idempotent and template
updates are surfaced per row ("shipped template updated - apply"), never
silently applied.

> **⚠️ Dispatch breakage window — placeholder digest (known GA follow-up).**
> As shipped, `BUNDLED_RUNNER_IMAGE_REF` is the **all-zero `sha256:0000…0000`
> placeholder** digest. A seeded or backfilled profile that still carries this
> digest **cannot provision a workspace** — the image does not exist in any
> registry, so a dispatch fails at container-create. This is **fail-loud by
> design, not silent**: resolving the dispatch route raises a typed
> `SandboxDispatchUnboundError` naming the placeholder digest and pointing here,
> instead of letting the run appear to complete and then error at pull time.
> Until the GHCR publish job lands the real released digest (or an operator
> pins a built digest into the template constants and re-seeds/applies), **every
> pipeline bound to a Bundled Runner profile fails at dispatch.** Critically, the
> migration `0191_bundled_runner_seed_backfill` re-points the legacy `modulo-dev`
> `local_docker` row to the Bundled Runner, so any pipeline that previously ran
> on E2B via `modulo-dev` will **also fail at dispatch** on this branch until the
> digest is provisioned. Remediation before relying on it: either wait for the
> GA digest bump, or self-build the runner image
> (`deploy/docker/runner-opencode.Dockerfile`), pin its digest in
> `bundled_runner_template.py`, and re-seed/apply.

## 4. The seeded profile

Every org gets a **"Bundled Runner (Docker)"** environment profile at
org-creation (owned by the org's admin account). Orgs that predate the hook
receive it via the one-time migration `0191_bundled_runner_seed_backfill`,
which also re-points the legacy `modulo-dev` `local_docker` row to the
Bundled Runner (release note covers the change; the rollback path restores
the row, and the inserted backfill rows are left in place — additive).

Locked property: **`persistence_policy` is `ephemeral`** — the CRUD
validator rejects `retained`/`cache` for `runner_docker` (dispatch
re-checks). Runner workspaces are throwaway by design.

Template drift is computed live against the shipped constants; the Runners
page (D5) will surface per-row "shipped template updated - apply" and the
apply action refreshes template-owned fields (provider, digest, hardening,
network defaults) while preserving operator-owned ones.

## 5. What runs where (network shape)

- Backend + SAQ + proxy: the compose default network.
- Workspaces: the `modulo-runner-workspace` bridge ONLY — they cannot reach
  the proxy or backend services (verified + docker-marked-asserted).
- Egress default: permitted (the tier's purpose). Per-profile opt-out:
  `network_policy: none` → `--network=none` (loopback only).
- Host-published ports (DB/Redis in the default compose) are reachable from
  egress-permitted workspaces — see the trust-boundary doc for the
  hardening criterion and operator mitigations.

## 6. Reconciler (leak repair)

A system cron sweeps labelled workspace containers every 5 min:

- Machine-scoped by the deployment-identity label (`modulo.machine.id`, set
  from `MODULO_RUNNER_MACHINE_ID`, hostname fallback) — two deployments
  sharing one engine never destroy each other's workspaces. Set
  `MODULO_RUNNER_MACHINE_ID` when you run more than one deployment against
  a shared engine.
- Orphans (run no longer active) are destroyed after a 5-minute grace
  period; the destroy path re-checks run status first and aborts with
  `runner.reconciler.suspected_false_positive` if the run went active again.
- **Soak mode first**: `RUNNER_RECONCILER_DESTROY_ENABLED` defaults to
  `false` — orphans are logged loudly (`runner.reconciler.orphan_detected`)
  for at least one soak period before you flip it to `true` in production.
- Fail-safe: any cross-reference query error aborts the sweep, destroys
  nothing, and emits `runner.reconciler.sweep_aborted` (the SAQ cron retries
  with partial counts persisted).
- 24h max-lifetime backstop: a labelled container older than 24h is
  reclaimed regardless of run state (`runner.workspace.reclaimed_max_lifetime`).
- Liveness: `/healthz/ready` exposes the advisory `runner_workspace_reconcile`
  check from the shared sweep-stats key — a dead sweep degrades, never gates.

## 7. Verifying the install

1. Through-proxy sanity (any HTTP client inside the backend container):
   `GET http://docker-socket-proxy:2375/_ping` -> `OK`;
   `GET http://docker-socket-proxy:2375/networks` -> `403` (allowlist proof).
2. Run a `sandbox_agent` node on the seeded profile (llm mode with a stub
   model backend or script mode). Watch: workspace container appears on
   `modulo-runner-workspace` with the hardening config (read-only rootfs,
   dropped caps, uid 1001), live output streams mid-exec, output.json
   collected, container destroyed at teardown.
3. Kill the engine (or `docker kill` the workspace) mid-exec: the node fails
   RETRYABLE — never as a completed run with a fabricated zero exit.
4. Docker-marked acceptance suite (requires a live engine):
   `uv run pytest tests/docker -m docker` from `backend/`
   (`MODULO_RUNNER_DIND_TESTS=1` adds the dind engine-kill strip, ~560 MB).

## 8. Remote engine

Point `MODULO_DOCKER_HOST` at a remote engine's TCP endpoint and deploy the
same socket proxy in front of it (the overlay's proxy is host-agnostic).
Everything else — hardening, labels, reconciler scoping — behaves
identically; the reconciler's machine-identity label isolates multiple
deployments sharing one engine.

## 9. Rollback

- Disable the tier: unset the `runner` profile (the overlay is opt-in) —
  bound profiles then raise the typed dispatch-unbound error (fail-closed,
  no silent activation).
- The overlay's services are stateless; removing them costs nothing.
- The backfilled profile rows are additive and survive a migration
  downgrade by design.
- One-way door: a GHCR-published image once pulled + pinned becomes the
  deployment's runner identity (see ADR 029's one-way door table).

## 10. Health probe (Runners page status strip)

A per-machine system cron (`runner_health_probe`, every 60s) probes the
engine THROUGH the same endpoint chain (`MODULO_DOCKER_HOST` →
`DOCKER_HOST` → local socket) and caches per-(org, machine) results in
the `runner_probe_cache` table: engine reachability, pinned-image
presence, and the engine's `/info` CPU/memory. **The Runners page reads
only this cache — it never probes the engine synchronously on the request
path.**

The probe's own liveness is observed like every other system cron:

- `/healthz/ready` carries the advisory `runner_health_probe` check
  (the `saq:cron:stats:runner_health_probe` outcome key) — a dead probe
  degrades, never gates; the per-machine FAR-538 cron heartbeat
  (`saq:cron:heartbeat:runner_health_probe`) also refreshes per tick.
- The probe prunes cache rows not refreshed within a 24h retention window
  (a decommissioned machine's corpse row cannot pin the strip to a
  permanent unknown); the read side bounds by the same window.

### Strip states — remediation

The persistent strip on the Runners page aggregates worst-of across the
org's machine rows:

| State | Meaning | Remediation |
|---|---|---|
| ✓ healthy | Engine reachable, pinned image present | — |
| ⚠ engine unreachable | The engine did not answer | Check `docker-socket-proxy` is running and `MODULO_DOCKER_HOST` resolves; the probe error text (scrubbed of any URL credentials) is shown inline |
| ⚠ image not pulled | A non-placeholder pinned digest has no image on the engine | Pull the pinned image on this machine (`deploy/docker/runner-opencode.Dockerfile`) or advance the digest (/§3) |
| stale | The cache is older than 2x the probe interval — the probe itself is suspended, the state is **never green** | Restart the SAQ system worker; the advisory `runner_health_probe` readiness check alerts too |

A healthy→unreachable transition emits a `runner_unavailable`
error-dashboard entry and an in-app notification (category `runner`)
linking to /admin/runners/concurrency; re-alerting requires a recovery in
between. A bounded aiodocker timeout (10s total / 5s connect) turns a hung
proxy into a recorded unreachable (a real alert) instead of a silent 120s
job timeout.

### Concurrency preflight

The Concurrency tab sizes the org's sandbox-concurrency cap against the
engine's reported `/info` resources (1.0 CPU / 1 GiB per container, worst
across machines): a cap whose `limit × 1 GiB` exceeds reported memory
surfaces `exceeds_mem` (analogous for CPU). `uncapped` = no cap set;
`unknown` = no recent probe result (no engine info cached yet).
