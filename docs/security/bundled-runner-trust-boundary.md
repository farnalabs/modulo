# Bundled Runner — trust boundary

<!-- FAR-590, D4 of the Agent Execution Tiers plan (ADR 029). Audience:
     anyone reasoning about what a sandbox_agent node can and cannot reach
     when it executes on the Bundled Runner (Docker) packaging. -->

The Bundled Runner executes `sandbox_agent` nodes in a hardened Docker
container on the deployment's own Docker engine. This document states the
trust boundary precisely: what the workspace can reach, what the backend's
Docker access can and cannot do, and which guarantees are enforced vs
documented.

## Components

| Component | Image | Trust role |
|---|---|---|
| Backend + SAQ workers | `modulo/backend` | Trusted: holds org credentials, drives the engine |
| Docker socket proxy | `lscr.io/linuxserver/socket-proxy` (digest-pinned in `deploy/compose/runner.yml`) | Filtered Docker API endpoint — accident prevention, NOT containment of a compromised backend |
| Runner workspaces | `modulo-runner:opencode` (first-party, digest-pinned per minor) | Untrusted: runs agent-authored commands/LLM sessions |
| Workspace network | `modulo-runner-workspace` (compose-declared bridge) | The ONLY network a workspace joins |

## Workspace egress surface (what an agent can reach)

A workspace container is created attached to the dedicated
`modulo-runner-workspace` bridge ONLY. It never joins the compose/backend
network, and the Docker endpoint (socket proxy) exists only on the backend
network — a workspace resolves no `docker-socket-proxy` DNS name
(engine-guaranteed, hard-asserted). Cross-network IP reachability
additionally relies on the ENGINE's bridge isolation rules: verified in the
phase-0 spike (name-blocked AND IP-blocked), and re-asserted by the
docker-marked harness suite — but the IP leg is engine-dependent. The
harness runs a control experiment first and skips it with an explicit
notice when an engine demonstrably permits cross-bridge traffic (observed
2026-09-10 on Docker Desktop engine 29.7.2, which no longer installs the
DOCKER-ISOLATION ruleset). Operators on engines without bridge isolation
must firewall the proxy endpoint (or the workspace subnet) at the host for
the containment claim to hold.

**Egress default: permitted.** The tier's purpose is an agent with network
access (git, package registries, model APIs). A per-profile opt-in sets
`network_policy: none`, which provisions the workspace with
`--network=none` (loopback only). Per-profile egress allowlists are deferred
(tracked separately).

**Host-published ports are reachable from workspaces.** The default compose
publishes Postgres (5432) and Redis (6379) on host ports; an
egress-permitted workspace can target the host and therefore those ports.
The hardening remedy is a named acceptance criterion with a CI guard (not
best-effort): the default compose rebinds to `127.0.0.1:` host bindings, and
any port that cannot be rebound is enumerated here with the reason.
Operator mitigation until that guard lands: firewall the host ports from the
Docker bridge subnets, or run the engine on a separate host. (The `saq-system`
SAQ web UI already binds `127.0.0.1:8081` in the base compose.)

## Docker endpoint filtering (the socket proxy)

When the overlay is enabled, the backend's Docker endpoint is the filtered
proxy (`MODULO_DOCKER_HOST=tcp://docker-socket-proxy:2375` — step 2 of the
documented endpoint chain). The raw socket is a documented operator override
(step 3/4: `MODULO_DOCKER_HOST` unset + `DOCKER_HOST`, or no env at all →
local socket). The allowlist is derived mechanically from the backend's real
Docker API surface and is TESTED, not claimed: the docker-marked
completeness assertion runs the provider exercise through the proxy and
fails on ANY request the allowlist rejects.

### Allowlist (endpoint -> proxy config mapping)

| Proxy config | Paths opened | Used by |
|---|---|---|
| `CONTAINERS=1` | `^/containers/*` — list/create/inspect/start/stop/remove, exec-create | Provision, destroy, exec-create, reconciler listing (label filters) |
| `EXEC=1` | `^/exec/*` — exec-start (hijacked stream), exec-inspect | Streaming + collect exec |
| `IMAGES=1` | `^/images/*` — inspect/list/create (pull) | Provision pulls; also serves the deprecated shell connector's `python:3.12-slim` default — its surface is inside the completeness assertion so enabling the overlay cannot silently re-home the connector |
| `PING=1` / `VERSION=1` / `INFO=1` | `^/_ping`, `^/version`, `^/info` | Health + engine-shape probes |
| `POST=1` | all non-GET methods, globally | Create/start/exec/destroy need it. NOTE: DELETE also passes when `POST=1` (the method gate is "GET or POST-flag", not per-method) — container destroy needs it; every other DELETE is residual-only because the volumes/networks/swarm categories stay closed |
| `ALLOW_ARCHIVE=0` etc. | linuxserver deny-refinements | archive/export/logs/top/change denied even with `CONTAINERS=1` |

No `networks/*` endpoints are needed: the workspace network is
compose-defined and containers attach to it at create-time via
`NetworkingConfig` (phase-0 verified; this is also why the overlay's holder
service must guarantee the network exists — compose does not create
unreferenced networks under `--profile`).

### Residual exposure (per-category granularity is partial)

The proxies are per-CATEGORY, not per-endpoint. With the production config,
beyond the allowlist above, these stay reachable (enumerated per the phase-0
spike, linuxserver base — archive/export/logs/top/change already denied):

- `/containers/{id}/{attach,rename,update,resize,stats,wait,changes,kill,restart,pause,unpause}` and `/containers/prune`
- `/exec/{id}/resize`
- `/images/prune`, `/images/{name}/tag`
- all DELETE verbs whose prefix category is on (container delete is needed;
  image delete rides `IMAGES`; volume/network/swarm deletes stay closed
  because those categories are off)

The provider's own use of this surface is exactly: create, start, exec
(create/start/inspect), inspect, list (label-filtered), destroy. Anything
beyond that in the residual list is not exercised by Modulo and exists only
because category granularity cannot express "exec-create without the rest of
the containers category". The escalation path (a minimal in-repo allowlist
proxy with per-endpoint rules) is the plan's named contingency if this
residual exposure is judged unacceptable.

**Endpoint filters do not filter request payloads.** The proxy is
accident-prevention: it stops the backend's Docker client from straying off
the derived surface. It is NOT containment of a compromised backend — a
compromised backend holds org credentials and could use any allowed path
maliciously (e.g. exec into an arbitrary container). The proxy also cannot
prevent the backend from reading env-injected credentials via container
inspect (documented tradeoff; mitigations are the same ones that protect
against any backend compromise: keep secrets out of container env where the
threat model demands it).

**Hijacked-stream timeout**: exec-start streams inherit HAProxy's 10-minute
inactivity timeout through the proxy — a silent exec longer than that is cut
by the proxy. This is consistent with (and backup to) Modulo's own stall
detection, which kills the exec first.

**Weekly scan cadence**: the proxy image and the runner image are digest-
pinned and ride the weekly trivy cadence on published tags (new high/critical
opens an issue; off-cycle rebuild for critical CVEs). The digest-drift guard
(the release job asserts the seed/docs digest constant matches the latest
`released-<minor>` tag) is a GA/CI item; until it exists, digest advances are
manual and the pinned digests in `deploy/compose/runner.yml` +
`modulo/db/bundled_runner_template.py` are the source of truth.

## Registration env matrix

| Environment state | `runner_docker` registered? |
|---|---|
| Any `MODULO_RUNNER_*` variable set (e.g. `MODULO_RUNNER_MACHINE_ID`) | YES — the overlay sets `MODULO_DOCKER_HOST`, which is itself a `MODULO_*`-prefixed signal |
| `MODULO_DOCKER_HOST` or `DOCKER_HOST` set | YES |
| None of the above | NO — `runner_docker` does not register, regardless of anything else; `DOCKER_HOST` alone (unset) registers nothing new and the legacy behaviour is unchanged |

An unregistered-but-bound provider raises `ProviderNotConfiguredError`
(surfaced as the typed dispatch-unbound error) naming `MODULO_DOCKER_HOST`
— never a silent fallback.

## What is NOT (yet) enforced

Honest inventory of D4 boundaries that are documented rather than
mechanically guarded here:

- **Digest-drift guard + GHCR publish** (structure test, trivy/grype fail
  high/critical, cosign/SBOM attest) — GA/CI item; digests are pinned but
  the automated publish/verify pipeline does not exist yet.
- **The compose overlay CI guard** (no `privileged: true`, no host
  network/pid/ipc, no unrestricted `cap_add` in overlay services; no host
  ports for the proxy) — the overlay conforms today; the CI job that
  enforces it is the same GA/CI item.
- **The default-compose `127.0.0.1:` rebind** — acceptance criterion above;
  a CI guard is the named remedy.
- **Egress allowlists per profile** — deferred (the `none` opt-in IS
  enforced at provision).
