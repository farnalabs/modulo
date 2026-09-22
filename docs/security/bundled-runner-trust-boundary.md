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
| Workspace network | `modulo-runner-workspace` (compose-declared bridge) | The ONLY network a workspace joins — validated at CRUD, dispatch, and provider boundary (FAR-1020) |

## Execution-tier model (where T1 sits)

The execution tiers form a convenience↔control spectrum: Tiers 1–3 are all
"Modulo provisions a workspace and runs the agent in it", differing only in
WHERE the compute lives. Tier 4 is a variant with a different payload (whose
agent runs).

| Tier | Shape | Status |
| -- | -- | -- |
| **T1** Bundled Runner (self-hosted Docker) — this document | simplest, self-contained; **deliberately permissive networking** — bounded egress is the customer's cluster's job, not a bespoke module of ours | shipped + hardened |
| **T2a** Managed sandbox (E2B, and the adapter pattern for Daytona et al.) | zero-setup external compute | E2B shipped |
| **T2b** Bundled Runner on rented compute (Hetzner/Ubicloud/any Docker host) | *not a new tier* — T1 on rented metal, works via `MODULO_DOCKER_HOST`; needs validation + docs, not an adapter | doc/validation |
| **T3** Modulo runner on Kubernetes | **most recommended shape** — inherits the customer's RBAC, admission policy, NetworkPolicy and workload identity; bounded by *their* controls | to build |
| **T4** Bring-your-own agent image | Modulo provisions the workspace, runs **the customer's** agent image; same machinery, different payload + result contract | to build, after T3 |
| **Dispatch** | govern an agent you already run — external CI triggers and customer-hosted agent endpoints | separate spike; connectors story, not a runner tier |

**T3 is the bounded-egress answer.** When an operator needs the workspace's
egress bounded to an allowlist, the supported path is T3: the customer's own
NetworkPolicy on their Kubernetes cluster enforces the bound. There is no
bounded middle tier in Docker (T1/T2b), by decision.

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

**Egress default: permitted — by design.** The tier's purpose is an agent
with network access (git, package registries, model APIs), so workspace
egress is unrestricted by design. A per-profile opt-in sets
`network_policy: none`, which provisions the workspace with
`--network=none` (loopback only).

**Accepted gap: no bounded egress in the Docker tier.** There is no egress
allowlist in this tier. This is a DECISION, not an omission: a vendor-built
egress gateway (an nftables or proxy middlebox owning enforcement) would be
the same criticised shape as the socket proxy — a bespoke module owning a
control the operator should own. The bounded answer is the Kubernetes tier
(T3, in the model above): run Modulo's runner on your cluster and bound the
workspace with YOUR NetworkPolicy, RBAC, and admission policy. There will be
NO bespoke egress gateway. This decision supersedes FAR-1039 (the earlier
per-profile allowlist plan is dropped, not deferred).

**workspace_network validation (FAR-1020).** The `workspace_network` value in
an environment profile's `config_json` is validated at three layers:

1. **CRUD boundary** — the `create_environment_profile` and
   `update_environment_profile` CRUD functions reject dangerous values
   before any DB write.
2. **Dispatch** — `_workspace_spec_for_dispatch` validates the value read
   from the DB, so a value written directly (bypassing the API) cannot
   reach the container.
3. **Provider** — `DockerRuntimeProvider._resolve_network_mode` validates
   the final resolved value before it becomes `HostConfig.NetworkMode`.

The deny-list explicitly rejects: `host` (host network namespace),
`container:*` (shares another container's network), `bridge` (default Docker
bridge), `none` (opt-in via `egress_policy` instead), and `default` (Docker
alias for `bridge`). Only plain deployment-owned bridge network names are
accepted. The validation function `validate_workspace_network` lives in
`modulo.util` and is the single source of truth.

**Host-published ports are loopback-bound (enforced).** The default compose
rebinds all host-published ports to `127.0.0.1:` (FAR-1035). An
egress-permitted workspace targeting the host hits only the loopback
interface and cannot reach Postgres, Redis, or the app server on the host.
A guard test (`test_compose_loopback_ports`) verifies this on every CI
run and fails if any default compose file publishes a port on a non-loopback
address.

The sole exception is `deploy/compose/docker-compose.prod.yml`'s
`${PORT:-80}:80` binding on the `modulo` service — the production app
must be reachable from outside.  That file is excluded from the guard;
its port policy is documented here, not in the test.

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
| `ALLOW_ARCHIVE=0` etc. | linuxserver deny-refinements | archive/export/logs/top/change denied even with `CONTAINERS=1` — the denial (403 + the `PR--` proxy-reject flag in the proxy log) is asserted by the docker-marked harness matrix probes on a real running container, so widening `ALLOW_LOGS` (etc.) in production FAILS CI |

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
`modulo/db/bundled_runner_template.py` are the source of truth. The CI
rig's proxy pin in `deploy/compose/runner-ci.yml` is ALIGNED to production's
digest — digest bumps must move the two compose files together, asserted by
the harness composition guard
(`test_overlay_matches_prod_proxy_config`).

## Registration env matrix

| Environment state | `runner_docker` registered? |
|---|---|
| Any `MODULO_RUNNER_*` variable set (e.g. `MODULO_RUNNER_MACHINE_ID`) | YES — the overlay sets `MODULO_DOCKER_HOST`, which is itself a `MODULO_*`-prefixed signal |
| `MODULO_DOCKER_HOST` or `DOCKER_HOST` set | YES |
| None of the above | NO — `runner_docker` does not register, regardless of anything else; `DOCKER_HOST` alone (unset) registers nothing new and the legacy behaviour is unchanged |

An unregistered-but-bound provider raises `ProviderNotConfiguredError`
(surfaced as the typed dispatch-unbound error) naming `MODULO_DOCKER_HOST`
— never a silent fallback.

## Container hardening (enforced defaults)

Every workspace container is provisioned with these confinement mechanisms
applied at create-time. They are NOT optional per-profile — they are
hardened defaults that cannot be silently widened.

### Non-root user (FAR-1036)

Every workspace container runs as uid 1001:1001 by default, regardless of
the image's declared user. This is the single biggest containment primitive
against a compromised agent session.

**Opt-out**: set `spec.allow_root_user = True` on the profile to skip the
non-root stamp. This is logged as a WARNING (`running as root
(allow_root_user=True) — this weakens the security boundary`). Images that
genuinely cannot run as an arbitrary uid (e.g. images that bind-mount
host-owned paths owned by root) must use this opt-out; the opt-out is
explicit and auditable.

### Seccomp profile (FAR-1037)

Every workspace container asserts Docker's built-in default seccomp profile
(`seccomp=builtin`). This blocks ~44 syscalls that are unnecessary
for container workloads (e.g. `mount`, `reboot`, `ptrace`).

`builtin` is the daemon sentinel that selects the built-in default profile
(moby's `config.SeccompProfileDefault`), not a profile name: the daemon
JSON-decodes any other non-`unconfined` value as an inline profile, so a
value such as `seccomp=default` fails container creation with
"Decoding seccomp profile failed".

The profile is **non-relaxable**: the `_SECURITY_OPT` constant includes
`seccomp=builtin` and the `_build_container_config` method copies it
verbatim. A configuration that widens it (e.g. `seccomp=unconfined`) would
require modifying the source constant, which is a code change visible in
review.

### AppArmor confinement (FAR-1037)

Every workspace container asserts AppArmor's default profile
(`apparmor=docker-default`). On hosts with AppArmor enabled, this confines
the container to the standard Docker profile (file network mediation,
capability restrictions). On hosts without AppArmor, Docker silently ignores
the `apparmor=` option — this is a documented degradation, not a silent
failure: the seccomp profile still applies.

### Other hardening defaults

- `no-new-privileges:true` — prevents privilege escalation via setuid binaries
- `cap_drop: ALL` — drops every Linux capability
- `read-only rootfs` with tmpfs `/home/user` (512 MB) and `/tmp` (128 MB)
- 1.0 CPU / 1 GiB resource limits

## Docker endpoint TLS (FAR-1038)

The Docker endpoint (`MODULO_DOCKER_HOST` / `DOCKER_HOST`) is validated at
provider registration time:

| Endpoint type | TLS required? | Rationale |
|---|---|---|
| `None` or unset | No | Default local socket — no network transit |
| `unix://...` | No | Local socket — no network transit |
| `tcp://localhost:2375` | No | Loopback — does not traverse a network |
| `tcp://127.x.y.z:PORT` | No | IPv4 loopback — does not traverse a network |
| `tcp://[::1]:PORT` | No | IPv6 loopback — does not traverse a network |
| `tcp://docker-socket-proxy:2375` | No | Shipped compose-internal proxy on a private bridge |
| Any other `tcp://...` | **Yes** | Remote TCP carries exec streams, inspect responses, and env-injected credentials in cleartext |

**What counts as "local"**: unix sockets, loopback TCP endpoints
(`localhost`, `127.x.y.z`, `::1`), and the shipped compose-internal hostname
on the shipped port (`tcp://docker-socket-proxy:2375`). A bare hostname on
the compose network (`tcp://my-service:2375`) is NOT treated as local — it
could be a different host on a different network segment. The
`docker-socket-proxy` exemption is pinned to port `2375`: the same host on
any other port is remote and requires TLS. If in doubt, the rule requires TLS.

**Single enforcement point**: the same validation runs for BOTH Docker
consumers — the runtime provider at registration and the orphan reconciler
when it constructs its engine client — so a remote cleartext endpoint cannot
slip through one path while being rejected on the other.

**Escape hatch**: operators who need a non-loopback, non-TLS endpoint (e.g. a
socket proxy on a private bridge) can set
`MODULO_DOCKER_ALLOW_INSECURE_ENDPOINT=1`. This logs a prominent warning at
provider construction but permits the endpoint. See
`docs/security/bundled-runner-operator-guide.md` §8 for caveats.

**How TLS is detected**: the validation checks `DOCKER_TLS_VERIFY` and
`DOCKER_CERT_PATH` environment variables. A remote endpoint without either
set is rejected at registration with an actionable error:

```
Remote Docker endpoint 'tcp://remote-host:2375' requires TLS.  Set
DOCKER_TLS_VERIFY=1 and DOCKER_CERT_PATH to a directory containing
client certificates (cert.pem, key.pem, ca.pem), or use a local unix
socket / the compose-internal proxy instead.
```

**Supported TLS configurations**:

- **Server TLS** (one-way): `DOCKER_TLS_VERIFY=1` + `DOCKER_CERT_PATH`
  containing `ca.pem`. Verifies the remote engine's certificate.
- **Mutual TLS** (two-way): `DOCKER_TLS_VERIFY=1` + `DOCKER_CERT_PATH`
  containing `ca.pem`, `cert.pem`, and `key.pem`. Both client and server
  authenticate.

See `docs/security/bundled-runner-operator-guide.md` §8 for operator-facing
setup instructions.

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
- **Bounded egress allowlists per profile** — deliberately NOT built for
  this tier; this is an ACCEPTED GAP (see "Accepted gap" above), not an
  omission. The `none` opt-in IS enforced at provision. The bounded answer
  is the Kubernetes tier (T3) with the customer's own NetworkPolicy; no
  bespoke egress gateway will be added (decision supersedes FAR-1039).

### Enforced

- **Default-compose `127.0.0.1:` rebind** — enforced by guard test
  `test_compose_loopback_ports` (FAR-1035).  Every host-published port in
  `docker-compose.yml`, `docker-compose.local.yml`, and
  `deploy/compose/docker-compose.test.yml` must bind to `127.0.0.1`.
  The sole documented exception is `deploy/compose/docker-compose.prod.yml`
  (`${PORT:-80}:80` on the `modulo` service — the production app must be
  externally reachable).
