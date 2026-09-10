# runner-ci compose overlay — Bundled Runner CI harness (FAR-773)

`deploy/compose/runner-ci.yml` is a **standalone** compose test rig for the
Bundled Runner's real topology: app → socket-proxy → Docker engine. It is not
merged with the root `docker-compose.yml` and defines no Modulo services.

## What it boots

| Service | Role |
|---|---|
| `docker-socket-proxy` | `lscr.io/linuxserver/socket-proxy`, **digest-pinned**, socket mounted `:ro`, production allowlist (`CONTAINERS/EXEC/IMAGES/INFO/PING/VERSION/POST=1` + the linuxserver deny-refinements `ALLOW_ARCHIVE/EXPORT/LOGS/TOP/CHANGE=0`, `LOG_LEVEL=info`). Publishes a random **loopback-only** port. |
| `dind` | `docker:28-dind`, `--privileged`, `DOCKER_TLS_CERTDIR=` empty, dockerd on unix + `tcp://0.0.0.0:2375`, loopback-only publish. The nested engine the suite's engine-kill strip and in-dind round-trip target. |
| `workspace-network-holder` | busybox no-op that exists only so compose materializes the `workspace` network — `compose --profile` never creates unreferenced networks (phase-0 spike surprise #1). |

Networks: `harness` (proxy + dind) and `workspace` (the dedicated runner
workspace bridge; the proxy never attaches to it).

## Usage

```bash
docker compose -f deploy/compose/runner-ci.yml --profile runner-ci up -d
docker compose -f deploy/compose/runner-ci.yml --profile runner-ci port docker-socket-proxy 2375
# -> 127.0.0.1:<port>  (same for the dind service)

# from backend/, with MODULO_DOCKER_HOST=tcp://127.0.0.1:<proxy-port>
uv run pytest tests/integration/test_bundled_runner_harness.py -m docker --tb=short -q --timeout=600

docker compose -f deploy/compose/runner-ci.yml --profile runner-ci down -v --remove-orphans
```

The CI job in `.github/workflows/ci.yml` ("Bundled Runner harness") does
exactly this; the weekly schedule backstop runs it unconditionally.

## Wall-clock budget (measured, Docker Desktop 29.7.2, 4 CPU / 17.5 GiB)

| Stage | Measured |
|---|---|
| `compose up` (cold: proxy + dind pulls) | ~105 s |
| `compose up` (warm) | ~5 s |
| docker-marked harness suite (9 tests, warm) | ~75 s |
| In-dind first `alpine:3.20` pull (first run only) | +~30 s |
| **Full harness job (cold)** | **~5 min** |
| Full harness job (warm) | ~3 min |

CI job `timeout-minutes: 15` (~1.5× the measured cold worst case with
headroom for the 2-vCPU hosted runner; revisit after the first CI run).

## Deviations from the phase-0 spike

- The spike used `POST=1`'s global method gate unchanged; identical here.
- The spike's scratch compose attached the proxy and backend to a shared
  "backend" network; here that network is named `harness` and the client is
  the pytest process on the host (via the loopback publish) — the CI runner's
  Docker daemon IS the engine under test, so no mock backend container is
  needed. The allowlist path exercised is identical.
- The spike pulled `latest` for reference digests; this overlay pins the
  current release digest
  `sha256:ba211325155c463a1a6e6a038c928f21447a8e60ce02ecf41302047974097e84`.

## HAProxy hijacked-stream timeout (documented constraint)

Exec-start's hijacked stream inherits HAProxy's `timeout client/server 10m`
(inactivity-based). A silent exec longer than 10 minutes through the proxy is
cut — consistent with Modulo's stall detection, but operator-facing docs
(`docs/security/bundled-runner-operator-guide.md`) should state it.
