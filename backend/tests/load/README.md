# Load Testing Framework (Locust)

Performance and load testing for the Modulo API using [Locust](https://locust.io/),
an open-source, Python-based load testing tool.

## Prerequisites

- **Locust** installed: `uv tool install locust` or `pip install locust`
- **websocket-client** for WebSocket users: `pip install websocket-client`
- Docker Compose stack running (Postgres 16, Redis 7, backend on port 8000)
- Test data seeded (see [Data Seeding](#data-seeding) below)
- Test user credentials present in the target environment (`admin@modulo.test` / `test-password-123`)

### Install Locust

```bash
# With pip
pip install locust websocket-client

# With uv
uv tool install locust
pip install websocket-client
```

## Data Seeding

Before running load tests, seed the test data:

```bash
cd backend
python -m tests.load.data_seed
```

This creates:
- 5 test pipelines with varying configurations (simple agent, sequential chain, HITL gate, high concurrency, long running)
- Trigger configurations (manual + cron) on the first pipeline
- 3 API keys for programmatic access

Flags:
```bash
python -m tests.load.data_seed --pipelines 10 --api-keys 5
python -m tests.load.data_seed --base-url http://localhost:8000/api/v1
```

## Running Tests

### Web UI (interactive)

```bash
cd backend
locust -f tests/load/locustfile.py
```

Open http://localhost:8089 in a browser. Set the number of users and spawn rate,
then start the test. Charts update in real time.

### Headless (CI / automated)

```bash
cd backend
locust -f tests/load/locustfile.py --headless \
  -u 50 -r 5 --run-time 5m \
  --host http://localhost:8000
```

### Headless with CSV export

```bash
locust -f tests/load/locustfile.py --headless \
  -u 50 -r 5 --run-time 5m \
  --csv results/load-test \
  --host http://localhost:8000
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BASE_URL` | `http://localhost:8000/api/v1` | API base URL |
| `ADMIN_EMAIL` | `admin@modulo.test` | Login email |
| `ADMIN_PASSWORD` | `test-password-123` | Login password |

## Test Scenarios

### PipelineRunUser (weight: 3, ramp 1→50)

Simulates the full pipeline lifecycle:
1. Creates a pipeline via `POST /api/v1/pipelines`
2. Triggers a run via `POST /api/v1/runs`
3. Polls `GET /api/v1/runs/{id}` until terminal status
4. Publishes the run ID for downstream consumers

**Purpose:** Validates the run execution path under concurrent load. Detects
bottlenecks in pipeline snapshot creation, executor dispatch, and DB polling.

**Metrics:** `pipeline_full_lifecycle` — includes creation + trigger + polling.

### HitlReviewUser (weight: 2, ramp 1→20)

Simulates human-in-the-loop review:
1. Picks a completed run from the shared waitlist
2. Lists pending HITL gates via `GET /api/v1/runs/{id}/hitl/pending`
3. Claims a gate via `POST /api/v1/runs/{id}/hitl/{gate}/claim`
4. Approves (80%) or rejects (20%) via the approve/reject endpoint

**Purpose:** Tests HITL claim concurrency, token expiry, and resume-after-approval
performance.

**Metrics:** `hitl_approve`, `hitl_reject`, `hitl_no_pending_gates`.

### WebSocketUser (weight: 1, ramp 1→10)

Simulates WebSocket event stream subscribers:
1. Acquires a WS token via `POST /api/v1/auth/ws-token`
2. Connects to `/api/v1/runs/{id}/ws` via WebSocket
3. Measures latency to first event
4. Listens for up to 15 seconds, counting events

**Purpose:** Validates WebSocket event broker throughput, auth token performance,
and concurrent subscriber scaling.

**Metrics:** `ws_first_event_latency` (critical), `ws_subscribe_session`.

### Mixed Workload

Since all user types run concurrently with their configured weights, the default
workload is always mixed. The 3:2:1 weight ratio produces roughly:
- 50% pipeline operations
- 33% HITL operations
- 17% WebSocket connections

## Interpreting Results

### Key Metrics

| Metric | Source | What it tells you |
|---|---|---|
| `pipeline_full_lifecycle` | Custom | End-to-end pipeline create → run → complete time |
| `hitl_approve` / `hitl_reject` | Custom | HITL claim + decision round-trip time |
| `ws_first_event_latency` | Custom | Time from WS connect to first received event |
| `ws_subscribe_session` | Custom | WS session duration and event count |
| `http_req_duration` | Built-in | Overall API response time across all endpoints |

### Locust Output Columns

| Column | Meaning |
|---|---|
| `# reqs` | Total requests of this type |
| `# fails` | Failed requests |
| `Avg` | Average response time (ms) |
| `Min` | Minimum response time (ms) |
| `Max` | Maximum response time (ms) |
| `p50` | Median response time (ms) |
| `p95` | 95th percentile (ms) |
| `p99` | 99th percentile (ms) |

### Expected Baseline Performance

| Operation | p50 | p95 | p99 | Error Rate |
|---|---|---|---|---|
| `pipeline_full_lifecycle` | <2s | <5s | <10s | <2% |
| `hitl_approve` | <500ms | <1s | <2s | <1% |
| `hitl_reject` | <500ms | <1s | <2s | <1% |
| `ws_first_event_latency` | <500ms | <1s | <2s | <2% |
| `POST /auth/login` | <200ms | <500ms | <1s | <1% |
| `POST /pipelines` | <200ms | <500ms | <1s | <1% |
| `POST /runs` | <500ms | <2s | <5s | <1% |
| `GET /runs/{id}` | <100ms | <300ms | <500ms | <1% |

### Thresholds

If p95 or error rate exceeds baseline, investigate:

1. **DB connection pool** — check `pgbouncer` or `max_connections`
2. **Redis** — check `maxclients` and memory pressure
3. **Backend CPU** — check container CPU throttling
4. **Alembic migrations** — ensure no unapplied migrations
5. **Connection limits** — check nginx/gunicorn worker count

## Adding New Scenarios

1. Create a new `TaskSet` class (or `HttpUser` subclass) in `locustfile.py`
2. Add a custom metric via `events.request_success.fire()` for end-to-end timing
3. Assign a `weight` to control the proportion of users
4. Update this document with the new scenario and baseline targets
5. Update `docs/operations/performance-baseline.md` with aggregate targets
