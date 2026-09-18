# runner-ci compose overlay — Bundled Runner CI harness (FAR-773)

`deploy/compose/runner-ci.yml` is a **standalone** compose test rig for the
Bundled Runner's real topology: app → socket-proxy → Docker engine. It is not
merged with the root `docker-compose.yml` and defines no Modulo services.

## What it boots

All three services are gated behind the `runner-ci` compose profile
(`profiles: ["runner-ci"]`) — invocation is unchanged: `--profile runner-ci`.

| Service | Role |
|---|---|
| `docker-socket-proxy` | `lscr.io/linuxserver/socket-proxy`, **digest-pinned to PRODUCTION's exact digest**, socket mounted `:ro`, production allowlist (`CONTAINERS/EXEC/IMAGES/INFO/PING/VERSION/POST=1` + the linuxserver deny-refinements `ALLOW_ARCHIVE/EXPORT/LOGS/TOP/CHANGE=0`, `LOG_LEVEL=info`). Publishes a random **loopback-only** port. |
| `dind` | `docker:28-dind` (**digest-pinned**), `--privileged`, `DOCKER_TLS_CERTDIR=` empty, dockerd on unix + `tcp://0.0.0.0:2375`, loopback-only publish. The nested engine the suite's engine-kill strip and in-dind round-trip target. |
| `workspace-network-holder` | `busybox:1.36` (**digest-pinned**) no-op that exists only so compose materializes the `workspace` network — `compose --profile` never creates unreferenced networks (phase-0 spike surprise #1). |

Networks: `harness` (proxy + dind) and `workspace` (the dedicated runner
workspace bridge; the proxy never attaches to it).

## Suite shape

`backend/tests/docker/test_bundled_runner_harness.py` (docker-marked, in the
standalone `tests/docker/` suite — no Postgres/testcontainers conftest chain):
**4 rig-backed tests** (allow/deny matrix + deny-refinement 403 probes with
the PR-- proxy-log assertion, nested round-trip + completeness log scan,
workspace-isolation probe, dind engine-kill) **+ 4 engine-free tests**
(two network-guard branch tests, two composition guards).

## Usage

```bash
docker compose -f deploy/compose/runner-ci.yml --profile runner-ci up -d
docker compose -f deploy/compose/runner-ci.yml --profile runner-ci port docker-socket-proxy 2375
# -> 127.0.0.1:<port>  (same for the dind service)

# from backend/, with MODULO_DOCKER_HOST=tcp://127.0.0.1:<proxy-port>
uv run pytest tests/docker/test_bundled_runner_harness.py -m docker --tb=short -q --timeout=600

docker compose -f deploy/compose/runner-ci.yml --profile runner-ci down -v --remove-orphans
```

## CI wiring

- `.github/workflows/ci.yml`'s path-filtered `runner-ci-harness` job calls the
  shared reusable `.github/workflows/runner-ci-harness.yml` (detect step
  filters on the Bundled Runner surface; `workflow_dispatch` runs
  unconditionally).
- `.github/workflows/runner-ci-backstop.yml` is the weekly drift backstop
  (cron `23 3 * * 1`): it calls the SAME reusable workflow with `force=true`
  so the path filter is skipped. The schedule deliberately does NOT live on
  ci.yml — a schedule trigger there would re-fire the ENTIRE fast-validation
  suite every week just to reach this one job.

## Wall-clock budget (measured, Docker Desktop 29.7.2, 4 CPU / 17.5 GiB)

| Stage | Measured |
|---|---|
| `compose up` (cold: proxy + dind pulls) | ~105 s |
| `compose up` (warm) | ~5 s |
| docker-marked harness suite (4 rig tests, warm) | ~75 s |
| In-dind first `alpine:3.20` pull (first run only) | +~30 s |
| **Full harness job (cold)** | **~5 min** |
| Full harness job (warm) | ~3 min |

CI job `timeout-minutes: 15` = **3x the measured local cold path**. The
2-vCPU hosted-runner multiplier is UNMEASURED until the first CI run —
revisit the budget after it (the local numbers come from a 4-CPU machine).

## Deviations from the phase-0 spike

- The spike used `POST=1`'s global method gate unchanged; identical here.
- The spike's scratch compose attached the proxy and backend to a shared
  "backend" network; here that network is named `harness` and the client is
  the pytest process on the host (via the loopback publish) — the CI runner's
  Docker daemon IS the engine under test, so no mock backend container is
  needed. The allowlist path exercised is identical.
- The spike pulled `latest` for reference digests; this overlay pins
  PRODUCTION's exact digest (the spike-verified linuxserver build,
  `sha256:cba12c42b1df30a6446856028f5ab726e9d8921f1ea74be509216c5e41357d42`,
  same as `deploy/compose/runner.yml`). Digest bumps must move BOTH files
  together — the composition guard
  (`backend/tests/docker/test_bundled_runner_harness.py::
  test_overlay_matches_prod_proxy_config`) asserts digest AND allowlist
  equality.

## HAProxy hijacked-stream timeout (documented constraint)

Exec-start's hijacked stream inherits HAProxy's `timeout client/server 10m`
(inactivity-based). A silent exec longer than 10 minutes through the proxy is
cut — consistent with Modulo's stall detection, but operator-facing docs
(`docs/security/bundled-runner-operator-guide.md`) should state it.
