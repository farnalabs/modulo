# Performance Baseline & Load Testing Reference

> See also: [Performance Overview](../performance.md) (k6 scripts),
> [Load Testing Framework](../../backend/tests/load/README.md) (Locust).

## Test Environment

| Component | Specification |
|---|---|
| **CPU** | 4 vCPU (AMD EPYC / Intel Xeon, 3.0+ GHz) |
| **RAM** | 16 GB |
| **Disk** | 256 GB NVMe SSD |
| **OS** | Ubuntu 22.04 LTS (Docker host) |
| **Docker** | 24.0+ with Compose V2 |
| **Postgres** | 16-alpine, shared_buffers=512MB, max_connections=200, work_mem=16MB |
| **Redis** | 7-alpine, maxmemory=256mb, maxmemory-policy=allkeys-lru |
| **Backend** | 4 uvicorn workers, 3 async DB pool connections per worker |
| **Python** | 3.12+ |
| **Network** | Loopback (localhost) |

## Methodology

1. **Warm-up** — 30s ramp to target user count
2. **Steady state** — 3 minutes at target load
3. **Cool-down** — 30s ramp down
4. All tests run against the Docker Compose stack on localhost
5. Test data is pre-seeded via `python -m tests.load.data_seed`
6. Each scenario runs 3 times; reported values are the median run

## Baseline Metrics (Locust)

Measured with 50 concurrent pipeline users, 20 HITL users, 10 WebSocket users.

### End-to-End Operations

| Operation | p50 | p95 | p99 | Error Rate | Notes |
|---|---|---|---|---|---|
| Pipeline lifecycle | <2s | <5s | <10s | <2% | Create + trigger + poll to completion |
| HITL approval | <500ms | <1s | <2s | <1% | Claim + approve round-trip |
| HITL rejection | <500ms | <1s | <2s | <1% | Claim + reject round-trip |
| WS first event | <500ms | <1s | <2s | <2% | Time from connect to first delta |
| WS session | <15s | <15s | <15s | <2% | Full subscribe + listen duration |

### REST Endpoints

| Endpoint | p50 | p95 | p99 | Error Rate |
|---|---|---|---|---|
| `POST /auth/login` | <200ms | <500ms | <1s | <1% |
| `POST /auth/ws-token` | <100ms | <300ms | <500ms | <1% |
| `POST /pipelines` | <200ms | <500ms | <1s | <1% |
| `GET /pipelines` | <100ms | <300ms | <500ms | <1% |
| `GET /pipelines/:id` | <100ms | <200ms | <400ms | <1% |
| `POST /runs` | <500ms | <2s | <5s | <1% |
| `GET /runs/:id` | <100ms | <300ms | <500ms | <1% |
| `POST /runs/:id/hitl/:gate/claim` | <200ms | <500ms | <1s | <1% |
| `POST /runs/:id/hitl/:gate/approve` | <300ms | <1s | <2s | <1% |
| `POST /auth/ws-token` | <100ms | <300ms | <500ms | <1% |

## Performance Budgets

| Resource | Budget | Hard Limit | Action if exceeded |
|---|---|---|---|
| DB connection pool | 80% of max_connections | 90% | Increase pool or scale DB |
| DB query time (p95) | <200ms | <500ms | Add index, tune query |
| Backend response (p95) | <1s | <3s | Profile, add caching |
| Redis memory | 60% of maxmemory | 80% | Increase maxmemory |
| WS subscriber count | 100 per run | 500 per run | Shard event broker |
| Concurrent runs | 50 | 100 | Increase workers |
| Error rate | <1% | <5% | Investigate and fix |
| CPU utilisation | <70% | <90% | Scale horizontally |
| Memory utilisation | <70% | <85% | Profile memory usage |

## Historical Trend Tracking

After each load test run, record the following in a central location (Grafana / Datadog / spreadsheet):

- Date and test run ID
- Number of users (pipeline / HITL / WS)
- p50, p95, p99 for each custom metric
- Error rate per operation
- Average CPU and memory during steady state
- DB query latency (from pg_stat_statements or slow query log)

## Regression Detection

**Immediate action** — if any metric exceeds its hard limit:

1. Check recent deployments vs `git log` on main
2. Run `git bisect` if a clear regression commit exists
3. Check Postgres `EXPLAIN ANALYZE` on slow queries
4. Review recent Alembic migrations for missing indexes
5. Check Redis `INFO` for evictions or blocked clients

**Scheduled investigation** — if p95 degrades by >20% week-over-week:

1. Profile the slowest endpoints with py-spy / async-profiler
2. Review DB query plans via auto_explain
3. Check connection pool wait events
4. Review OTEL traces for upstream dependency latency
