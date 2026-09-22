# Bundled Runner on rented compute (T2b)

<!-- FAR-1049 (T2b of the Agent Execution Tiers plan, ADR 029). Audience:
     operators who want the Bundled Runner to execute on a rented VM
     (Hetzner, Ubicloud, any Docker host) while Modulo itself runs
     elsewhere. Companion: docs/security/bundled-runner-operator-guide.md
     (§5 network shape, §7 verifying the install, §8 remote engine —
     TLS/mTLS, escape hatch, loopback exemptions); docs/security/
     bundled-runner-trust-boundary.md (threat model, egress position). -->

> **⚠️ Validation status: walkthrough NOT executed end-to-end (FAR-1049, T2b).**
> This document is written from the Bundled Runner's **documented** requirements
> (operator guide §5, §7, §8; the trust-boundary doc; the FAR-1038 TLS gate).
> The live validation — provisioning a real VM at a provider and recording an
> actual end-to-end run — is **DEFERRED**: it needs a provider account and a
> VM, which the writing host does not have. **No step below has been executed
> against a real rented engine as part of writing this document.** Treat every
> command as expected-correct but unverified. When the validation run happens,
> record its date and results in this banner.

## 1. What T2b is (and is not)

Hetzner, Ubicloud, or any Docker host is **not a new execution tier**. It is
**T1 — the Bundled Runner — on rented compute**. It already works today:
point `MODULO_DOCKER_HOST` at the VM (with TLS, per FAR-1038, see operator
guide §8) and the Bundled Runner runs there, including from a Modulo
deployment running elsewhere. The deliverable for this shape is
documentation and validation, **not an adapter**.

### T2b vs T2a

| | **T2a — managed sandbox (E2B)** | **T2b — Bundled Runner on rented compute (this doc)** |
|---|---|---|
| Where the workspace runs | The vendor's managed sandbox fleet | A Docker engine on a VM **you** rent and administer |
| Setup | Zero infra — bind a profile and go | Provision VM, install Docker, deploy the socket proxy, configure TLS |
| Billing shape | Metered per second of actual sandbox use; ~zero when idle | Flat VM rate (hourly/monthly), the same whether idle or busy |
| Egress allowlist | **Yes** — node-level `egress_policy: selected` + `egress_allowlist`, enforced fail-closed inside the sandbox | **No** — same accepted gap as T1 (see §5) |
| Patching / ops | The vendor's problem | Your problem (kernel, Docker, TLS certs, proxy image bumps) |
| Which to choose | Bursty or light use; when you need a bounded egress allowlist **today** | Steady heavy use where a flat VM is cheaper than metered seconds; when workloads must stay on compute you control |

Both run the same `sandbox_agent` **node contract**. The container
hardening defaults (uid 1001, read-only rootfs, dropped caps,
seccomp/AppArmor — operator guide §7, trust-boundary doc "Container
hardening") are **Docker-tier-only**: the Docker/Bundled Runner tier
enforces them at container create-time, while the E2B managed sandbox's
isolation is vendor-managed. The difference is who owns the metal and who
owns the isolation (and the egress story).

## 2. Walkthrough

Runnable, honest steps. **Not yet executed end-to-end** — see the
validation-status banner above.

### Step 1 — Provision a VM at a mainstream provider

Any provider works; the requirement is a Linux VM you can run Docker on and
open a firewall for, e.g.:

- **Hetzner Cloud** — CX/CPX series, any region.
- **Ubicloud** — bare-metal-ish VMs, usage-metered.
- Any other provider (DigitalOcean, Linode, AWS EC2, a local Proxmox host…).

Size floor: the Bundled Runner provisions workspaces at 1.0 CPU / 1 GiB
(plus the host's own Docker overhead). A 2 vCPU / 4 GiB VM comfortably runs
a few concurrent workspaces. Note the concurrency preflight (operator guide
§10): the Concurrency tab sizes the org's sandbox cap against the engine's
reported `/info` resources at 1.0 CPU / 1 GiB per container, worst case.

Record the VM's public IP — Modulo will reach it over the network, so this
endpoint **requires TLS** (step 4).

### Step 2 — Install Docker on the VM

Standard upstream install on the VM (example for Debian/Ubuntu — follow the
current official instructions for your distro):

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # re-login for the group to take effect
docker version                     # client + server both present = OK
```

Verify the engine version you are actually running and note it — the
bridge-isolation caveat in §4 is engine-dependent, and you will verify it
there before trusting the containment claim.

### Step 3 — Deploy the socket proxy in front of the engine

Never expose the raw Docker socket over the network. Deploy the same
filtered proxy the overlay ships (`lscr.io/linuxserver/socket-proxy`,
digest-pinned in `deploy/compose/runner.yml`) on the VM, with the socket
mounted read-only and **no host-published ports** facing the public
interface — Modulo reaches it over a private/firewalled path or through the
TLS front you add in step 4.

The overlay's proxy config is host-agnostic: copy the `docker-socket-proxy`
service definition from `deploy/compose/runner.yml` (the `CONTAINERS`,
`EXEC`, `IMAGES`, `PING`, `VERSION`, `INFO`, `POST` and deny-refinement
environment block) into a small compose file on the VM and `docker compose
up -d`. The allowlist it enforces is documented in the trust-boundary doc
(“Allowlist (endpoint → proxy config mapping)”) and is tested, not claimed.

Also declare the workspace bridge on the VM — the dedicated
`modulo-runner-workspace` bridge network — so workspace containers have a
network to attach to at create-time:

```bash
docker network create modulo-runner-workspace
```

### Step 4 — TLS for the endpoint (NON-OPTIONAL for non-loopback)

**A non-loopback `tcp://` Docker endpoint requires TLS (FAR-1038).** A
remote engine endpoint without TLS is rejected at provider registration with
an actionable error, on every path (provider registration and the orphan
reconciler alike). Full configuration instructions — server TLS vs mutual
TLS (mTLS), `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH`, the daemon's
`--tlsverify` flags — are in the operator guide **§8 “Remote engine” →
“TLS configuration”**. Summary:

- **Mutual TLS (mTLS)** is the recommended production configuration: all
  three of `ca.pem`, `cert.pem`, `key.pem` in `DOCKER_CERT_PATH`; both ends
  authenticate. The daemon typically listens on `2376` (TLS) instead of
  `2375` (plaintext).
- **Server TLS** (one-way, `ca.pem` only) verifies the engine's certificate
  but does not authenticate the client — weaker, acceptable only where
  client authentication is provided some other way.
- **Loopback endpoints are exempt** (`tcp://localhost:2375`,
  `tcp://127.0.0.1:2375`, `tcp://[::1]:2375`, unix sockets, and the
  shipped compose-internal `tcp://docker-socket-proxy:2375`) because they do
  not traverse a network. A rented VM reached from a *different* host is
  never loopback — the exemption does not apply to this walkthrough.
- The `MODULO_DOCKER_ALLOW_INSECURE_ENDPOINT=1` escape hatch exists for
  isolated private bridges and logs a prominent warning; it is **not** the
  path for a public-IP rented VM. See operator guide §8 caveats.

### Step 5 — Point `MODULO_DOCKER_HOST` at the VM

On the Modulo deployment (wherever it runs — it does not need to be on the
VM), set the endpoint env vars following the documented 4-step chain
(`MODULO_DOCKER_HOST` → `DOCKER_HOST` → local socket):

```bash
export MODULO_DOCKER_HOST=tcp://<vm-public-ip>:2376   # TLS port
export DOCKER_TLS_VERIFY=1
export DOCKER_CERT_PATH=/etc/modulo/docker-certs      # ca.pem, cert.pem, key.pem
```

Set the same three variables on every process that touches Docker: the
`backend`, `saq-runner`, and `saq-system` services (the overlay sets them
on all three; a manual remote setup must too — see operator guide §2).

Additional operational env for a shared or multi-deployment engine:

- `MODULO_RUNNER_MACHINE_ID` — set it if more than one deployment shares
  the engine; the reconciler's machine-identity label then scopes each
  deployment's orphan sweep to its own workspaces (operator guide §6).
- The registration matrix (operator guide §2): `runner_docker` registers
  when any `MODULO_RUNNER_*` variable **or** a Docker endpoint env is set.
  With none set it does not register, and binding a profile raises a typed
  dispatch error naming `MODULO_DOCKER_HOST` — never a silent fallback.

**Runner image on the engine.** The pinned runner image must exist on the
VM's engine before dispatch can provision a workspace. Check the operator
guide §3 for the digest you are pinned to and the known placeholder-digest
dispatch-breakage window: until the GHCR publish job lands a real digest,
self-build the image on the VM
(`deploy/docker/runner-opencode.Dockerfile`), pin its digest per §3, and
re-seed/apply.

### Step 6 — Run a `sandbox_agent` node and collect its output

This is the same acceptance sequence as operator guide §7, driven over the
remote endpoint:

1. **Through-proxy sanity** from inside the backend container:
   `GET http://<proxy>:2375/_ping` → `OK`;
   `GET http://<proxy>:2375/networks` → `403` (allowlist proof).
   With TLS in front, use the TLS-enabled client equivalent
   (`docker --tlsverify -H tcp://<vm>:2376 version`).
2. Run a `sandbox_agent` node bound to the seeded **“Bundled Runner
   (Docker)”** profile (llm mode with a stub model backend, or script
   mode). Watch on the VM:
   - the workspace container appears on `modulo-runner-workspace` with the
     hardening config (read-only rootfs, dropped caps, uid 1001);
   - live output streams mid-exec;
   - `output.json` is collected;
   - the container is destroyed at teardown.
3. **Failure-path check:** kill the engine (or `docker kill` the workspace)
   mid-exec — the node must fail RETRYABLE, never as a completed run with a
   fabricated zero exit.
4. The Docker-marked acceptance suite (`uv run pytest tests/docker -m
   docker` from `backend/`) requires a live engine; run it against this
   engine once the live validation is performed.

Record the outcome of steps 1–3 in the validation-status banner when this
walkthrough is actually executed.

## 3. Operational requirements for a remote engine

A remote engine is not “the local setup, elsewhere”. These requirements are
non-negotiable for the shape to hold:

1. **TLS/mTLS on the endpoint (FAR-1038).** Non-loopback `tcp://` without
   TLS is rejected at registration. Configure per operator guide §8;
   mTLS recommended for production. Loopback exemptions do not apply when
   Modulo and the engine are different hosts.
2. **The socket proxy in front of the engine.** The raw socket is a
   documented operator override, never the default. The filtered proxy is
   accident-prevention for the backend's Docker client (it is *not*
   containment of a compromised backend — trust-boundary doc, “Docker
   endpoint filtering”). Keep the proxy's allowlist as shipped; widening it
   fails the docker-marked CI assertions.
3. **A dedicated host.** Run the runner engine on a VM used for this
   purpose, not on the host running unrelated production services. Rationale:
   a workspace with permitted egress on the same host as other services can
   reach host-published ports (the default compose rebinds them to
   `127.0.0.1`, FAR-1035, which blocks the loopback case — but a dedicated
   host removes the question entirely), and engine-level breakouts are
   engine-level. One deployment per engine, or set
   `MODULO_RUNNER_MACHINE_ID` when sharing (operator guide §6).
4. **The workspace bridge network.** Workspaces attach to the dedicated
   `modulo-runner-workspace` bridge ONLY, at create-time via
   `NetworkingConfig` — they never join the compose/backend network and
   never resolve the proxy's DNS name (engine-guaranteed, hard-asserted).
   Declare it on the VM (step 3). The `workspace_network` value is
   validated at three layers (CRUD, dispatch, provider — FAR-1020,
   trust-boundary doc); only plain deployment-owned bridge names are
   accepted.
5. **The engine-dependent bridge-isolation caveat — VERIFY IT (see below).**

### Bridge isolation is engine-dependent — verify, then mitigate

Cross-network IP reachability between the workspace bridge and the
backend/proxy bridge relies on the **engine's own bridge-isolation rules**
(`DOCKER-ISOLATION` iptables chains). This was verified in the phase-0
spike (name-blocked *and* IP-blocked) and is re-asserted by the
docker-marked harness suite — **but the IP leg is engine-dependent.** The
harness runs a control experiment first and skips the IP assertion with an
explicit notice when an engine demonstrably permits cross-bridge traffic.

**Observed broken:** the engine no longer installs the
`DOCKER-ISOLATION` ruleset — observed 2026-09-10 on Docker Desktop engine
29.7.2. On such engines, name resolution is still blocked (the workspace
resolves no proxy DNS name), but **IP-level reachability from a workspace
to the proxy/backend bridge is not blocked by the engine.**

**Verification step (run on the target engine before trusting the
containment claim):**

```bash
# iptables backend — expect DOCKER-ISOLATION-STAGE-1/2 chains:
sudo iptables -S | grep DOCKER-ISOLATION

# nftables backend:
sudo nft list ruleset 2>/dev/null | grep -i isolation
```

- **Chains present** → the engine installs bridge isolation; the IP leg of
  the containment claim holds.
- **No `DOCKER-ISOLATION` chains** → the engine does **not** enforce
  cross-bridge isolation. Do not rely on it.

**Mitigation (required when isolation is absent):** firewall the proxy
endpoint (or the workspace subnet) **at the host**, so a workspace cannot
reach the proxy/backend bridge by IP even without the engine rules. For
example, with `ufw`/`nftables` on the VM:

- allow the proxy/backend port only from the Modulo host's IP (not from the
  workspace bridge's subnet), and/or
- drop traffic sourced from the `modulo-runner-workspace` bridge subnet
  (e.g. `172.x.y.0/24` — check the actual subnet with
  `docker network inspect modulo-runner-workspace`) toward the default
  bridge and any host-published ports.

The same rule applies to the default compose's own network shape if you run
Modulo and the engine on one host: verify the engine, and firewall if the
ruleset is missing. The trust-boundary doc states this caveat as the
authoritative version — keep them in sync if either changes.

## 4. Cost shape — rented VM vs managed sandbox (T2b vs T2a)

So a reader can choose between T2a and T2b knowingly:

| Cost dimension | **T2b** rented VM | **T2a** managed sandbox (E2B) |
|---|---|---|
| Billing model | **Flat** — a fixed hourly/monthly VM rate | **Metered** — per second of actual sandbox use |
| Idle cost | Full price whether or not any pipeline runs | ~zero when nothing runs |
| Busy cost | Same flat rate up to the VM's capacity; over capacity = resize (a bigger flat rate) | Scales linearly with concurrent sandbox-seconds |
| Setup cost | One-off: VM, Docker, proxy, TLS, patching cadence | Zero infra setup |
| Ops cost | Ongoing: OS/Docker updates, TLS cert renewal, proxy image bumps, engine-version monitoring (see §3, bridge-isolation caveat) | None — the vendor operates the fleet |
| When it wins | Steady, heavy, predictable usage where a flat VM is cheaper than metered seconds; data-residency requirements that pin compute to a chosen provider | Bursty/light usage; spiky concurrency; when you need an **egress allowlist today** (T2a supports it, T2b does not — §5); when you don't want to operate a VM |

Rule of thumb: at low or bursty utilisation the metered T2a sandbox is
cheaper *and* lower-effort; at sustained high utilisation a modest flat VM
is usually cheaper — but T2b then carries the whole ops surface and the
accepted egress gap below. The egress-allowlist requirement is a
hard discriminator: if you need bounded egress now, T2b cannot provide it
(choose T2a, or wait for T3).

## 5. What a remote engine does NOT give you

**A remote engine is placement, not policy.** Moving T1 to a rented VM
changes *where* the workspace runs; it does not change *what the workspace
can reach*:

- **No per-workspace network policy.** The same accepted gap as T1: **there
  is no egress allowlist in the Docker tier.** Workspace egress is permitted
  by design (the tier's purpose is an agent with network access — git,
  package registries, model APIs); the per-profile opt-out is only
  `network_policy: none` → `--network=none` (loopback only).
- This is a **decision, not an omission**: a vendor-built egress gateway
  would own a control the operator should own. The bounded answer for
  self-hosted compute is the **Kubernetes tier (T3, still "to build")**:
  run Modulo's runner on your cluster and bound the workspace with *your*
  NetworkPolicy, RBAC, and admission policy. There will be no bespoke
  egress gateway (this supersedes FAR-1039 — dropped, not deferred).
- The managed-sandbox tier (**T2a / E2B**) is the separate case that
  *does* support bounded egress today: node-level `egress_policy:
  selected` + `egress_allowlist`, an enforced fail-closed allowlist inside
  the sandbox.
- Everything else in the trust boundary carries over unchanged: the
  accepted no-bounded-egress gap, the bridge-isolation caveat (§3 of this
  doc), the socket proxy's accident-prevention-not-containment position,
  and the hardening defaults — see
  `docs/security/bundled-runner-trust-boundary.md`.

**In one line:** T2b gives you T1's convenience on metal you rent, with
T1's deliberately permissive networking — bounded egress remains T3's job
(cluster controls) or T2a's (managed sandbox allowlist), never T2b's.

## 6. References

- `docs/security/bundled-runner-operator-guide.md` — §5 network shape,
  §6 reconciler, §7 verifying the install, §8 remote engine (TLS/mTLS,
  escape hatch, loopback exemptions), §10 health probe + concurrency
  preflight.
- `docs/security/bundled-runner-trust-boundary.md` — egress position,
  accepted no-bounded-egress gap, bridge-isolation caveat, allowlist
  table, FAR-1038 TLS matrix.
- `docs/operations/network-egress.md` — Modulo's own outbound-egress
  audit (components, not sandbox workspaces).
- `deploy/compose/runner.yml` — the shipped socket-proxy overlay and the
  `modulo-runner-workspace` network declaration.
