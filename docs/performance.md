# Performance Baseline & Load Testing

## Overview

Load tests are written for [k6](https://k6.io) – a developer-centric, open-source
load testing tool. Scripts live in `tests/performance/` and target the Modulo API
(`http://localhost:8000/api/v1`).

## Prerequisites

- [k6](https://k6.io/docs/getting-started/installation/) installed locally (v0.49+)
- The Modulo backend (and its dependencies: Postgres 16, Redis 8) is running
- Test user credentials exist in the target environment (`auth-burst.js` rotates
  through five of them so token caching does not distort the login numbers):
  - `admin@modulo.test`, `user-one@modulo.test`, `user-two@modulo.test`,
    `viewer@modulo.test`, `editor@modulo.test`
  - all with password `test-password-123`

### Install k6

**Windows (Chocolatey):**
```powershell
choco install k6
```

**macOS (Homebrew):**
```bash
brew install k6
```

**Linux (APT):**
```bash
sudo apt-get update && sudo apt-get install k6
```

**Docker:**
```bash
docker pull grafana/k6
```

## Running Tests

All tests accept `BASE_URL` as an environment variable (defaults to
`http://localhost:8000/api/v1`).

### Pipeline CRUD (ramps to 100 VUs)

```bash
k6 run tests/performance/pipeline-crud.js
```

With Docker:
```bash
docker run --rm -i -v "$(pwd)/tests/performance:/tests" \
  grafana/k6 run --env BASE_URL=http://host.docker.internal:8000/api/v1 \
  /tests/pipeline-crud.js
```

### Run Execution (ramps to 50 VUs)

```bash
k6 run tests/performance/run-execution.js
```

### Auth Burst (ramping arrival rate to 200 req/s)

```bash
k6 run tests/performance/auth-burst.js
```

### Audit Query (ramps to 50 VUs; page, cursor, and filtered queries)

```bash
k6 run tests/performance/audit-query.js
```

### Running All Tests

```bash
for script in tests/performance/*.js; do
  echo "=== Running $script ==="
  k6 run "$script"
done
```

## Baseline Targets

These are the aspirational per-endpoint numbers. The **enforced** gate is what
each script actually sets in its `options.thresholds` block: every script
asserts `p(95)` only, plus an `errors` rate below 1%. Where the two disagree,
the threshold is the one CI enforces.

| Endpoint / Operation | p50 | p95 | p99 | Error Rate | Script threshold (p95) |
|---|---|---|---|---|---|
| **Auth** | | | | | |
| `POST /auth/login` | <100ms | <300ms | <500ms | 0% | `login_duration` <300ms |
| **Pipelines** | | | | | |
| `POST /pipelines` | <200ms | <500ms | <1000ms | <1% | `pipeline_create_duration` <500ms |
| `GET /pipelines` | <100ms | <300ms | <500ms | <1% | `pipeline_list_duration` <500ms |
| `GET /pipelines/:id` | <50ms | <200ms | <400ms | <1% | `pipeline_get_duration` <500ms |
| `PATCH /pipelines/:id` | <100ms | <300ms | <500ms | <1% | `pipeline_update_duration` <500ms |
| `DELETE /pipelines/:id` | <100ms | <300ms | <500ms | <1% | `pipeline_delete_duration` <500ms |
| **Run Execution** | | | | | |
| `POST /runs` | <300ms | <1000ms | <2000ms | <1% | `run_create_duration` <1000ms |
| `GET /runs/:id` (poll) | <50ms | <200ms | <400ms | <1% | `run_poll_duration` <500ms |
| **Audit** | | | | | |
| `GET /admin/audit` (page) | <50ms | <200ms | <400ms | <1% | `audit_page_duration` <200ms |
| `GET /admin/audit` (cursor) | <50ms | <200ms | <400ms | <1% | `audit_cursor_duration` <200ms |
| `GET /admin/audit` (filtered) | - | - | - | <1% | `audit_filtered_duration` <200ms |

`auth-burst.js` additionally pins `http_5xx_errors` to `rate==0`, so any 5xx
fails the run even though the `errors` budget is 1%.

### Notes

- **p50 = median**: half of all requests complete faster than this value
- **p95**: 95% of requests complete faster than this value
- **p99**: 99% of requests complete faster than this value
- Targets are for a **staged test environment** (local dev). Production targets
  should be 2–3× tighter.
- If running against a production-like environment, ensure test users are
  excluded from billing/analytics.

## Interpreting Results

k6 outputs a summary like this after each run:

```
     http_req_duration.....: avg=145ms   min=12ms   med=98ms   max=2.3s
       { expected_response:true }........... avg=145ms   min=12ms   med=98ms   max=2.3s
     ✓ pipeline_create_duration............. avg=210ms   min=45ms   med=180ms  p(95)=420ms
     ✓ pipeline_list_duration............... avg=85ms    min=10ms   med=60ms   p(95)=220ms
     ✗ errors............................... 0.23% – ✓ 977 ✗ 3
     ✓ checks............................... 100.00% – ✓ 4890 ✗ 0
```

### Key Metrics

| Metric | What it tells you |
|---|---|
| `http_req_duration` | Overall API response time |
| Custom trends (e.g. `pipeline_create_duration`) | Per-operation timing |
| `checks` | Assertion pass/fail ratio – **should be 100%** |
| `errors` | Rate of failed operations that don't meet response shape expectations |
| `http_req_failed` | Network/HTTP-level failures (5xx, connection refused, timeouts) |

### Threshold Failures

If a threshold fails, k6 exits with a non-zero code. Common causes:

- **Response time thresholds fail**: the endpoint is slower than expected.
  Check DB connection pool, query performance, indexing, or caching.
- **Error rate threshold fails**: some operations are failing. Check server
  logs for 5xx, 4xx, or dropped connections.
- **0% error rate violations**: even a single failed request should trigger
  investigation. Check the specific check that failed.

## CI Integration

### GitHub Actions

```yaml
name: Load Test

on:
  schedule:
    - cron: '0 6 * * 1'  # Every Monday at 6am
  workflow_dispatch:

jobs:
  k6:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run k6 load tests
        uses: grafana/k6-action@v0.3.0
        with:
          filename: tests/performance/auth-burst.js
          flags: --env BASE_URL=${{ secrets.TEST_BASE_URL }}
```

### Grafana Cloud / k6 Cloud

```bash
k6 cloud tests/performance/pipeline-crud.js \
  --env BASE_URL=https://staging.modulo.app/api/v1
```

### Pre-merge Gate (optional)

Add a lightweight smoke run to CI to catch regressions:

```bash
k6 run --vus 5 --duration 30s tests/performance/pipeline-crud.js
```

Fail the pipeline if any threshold is crossed.

## Script Architecture

Each script follows the k6 best-practice pattern:

1. **`setup()`** – one-time pre-test: login, create seed data, return shared state
2. **`default()`** – per-VU iteration: the actual load test operations
3. **`teardown()`** – cleanup: delete seed data, close connections

Custom Trend metrics track per-operation timing separately from the global
`http_req_duration`. Thresholds enforce the baseline targets defined above.

## Not covered here: the Locust suite

These k6 scripts cover the HTTP CRUD, login, and run-dispatch paths. They do
**not** cover HITL claim/approve/reject, WebSocket event-stream latency, or
end-to-end run completion under load. Those three task sets live in the Locust
suite:

```bash
cd backend
locust -f tests/load/locustfile.py
locust -f tests/load/locustfile.py --headless -u 50 -r 5 --run-time 5m
```

Seeding is explicit (`python -m tests.load.data_seed`). See
[`docs/operations/performance-baseline.md`](./operations/performance-baseline.md)
for the Locust baseline metrics and the k6 versus Locust split.

## Adding New Tests

1. Create `tests/performance/<name>.js`
2. Define custom `Trend` metrics for each operation
3. Set `thresholds` for p95 and error rate
4. Use `setup()` for any seed data, `teardown()` for cleanup
5. Run locally to establish baseline before committing
6. Update this document with the new test's baseline targets
