# Operational Incident Response

**Audience:** Platform engineers, SREs, and on-call responders for Modulo
production deployments. This guide covers **operational** (non-security)
incidents — service degradation, infrastructure failures, and performance
events.

**Prerequisite reading:**
- `docs/deployment.md` — base deployment reference
- `docs/deployment-security.md` — security hardening baseline
- `docs/operations/self-hosted-admin.md` — emergency admin procedures
- `docs/operations/backup.md` — backup/restore procedures
- `docs/operations/admin-bypass.md` — checkpoint bypass procedures
- `docs/operations/performance-baseline.md` — performance budgets and baselines

**Security incidents** (SSO compromise, API key leak, RLS bypass, prompt
injection, data exfiltration, container CVE exploit, insider threat) follow
`docs/security/incident-response-playbook.md`. This document covers everything
else.

---

## 1. Severity Classification

### 1.1 Operational Severity Scale

| Severity | Definition | Blast Radius | Examples |
|----------|------------|-------------|----------|
| **Critical** | Complete service unavailability or data-loss risk | All users or all tenants | Database down, Redis down with queue loss, all backend replicas crash-looping, storage full |
| **High** | Significant service degradation for most users | Multiple tenants or a major feature | High latency on pipeline execution, queue backlog > 10 min, rate limiting false-positives, partial DB replica failure |
| **Medium** | Partial degradation affecting a subset of users or features | Single tenant or minor feature | Individual pipeline failures, slow dashboard queries, elevated error rate on a single endpoint |
| **Low** | Cosmetic or performance regression with no user-facing impact | Internal tooling or background job | Slow migration, stale cache, minor latency increase below performance budget |
| **Info** | Informational — no immediate action needed | None | Storage trending toward capacity, HPA scaling events |

### 1.2 Mapping to Security Severity

Operational severity maps independently from security severity. A Critical
operational incident (e.g., DB full) can co-exist with a Medium security
incident. Use the higher of the two for escalation urgency.

---

## 2. Escalation Paths

### 2.1 Contact Channels

| Channel | Purpose | Used For |
|---------|---------|----------|
| `#ops-alert` (Slack) | Automated alerts from monitoring + initial human triage | All severities |
| `#ops-on-call` (Slack) | Dedicated incident channel (created per incident) | Critical, High |
| PagerDuty | Phone/SMS push notification for on-call engineer | Critical, High |
| `ops@modulo.run` | Email archive, compliance trail | All severities (CC on closure) |
| Signal / phone tree | Out-of-band contact when Slack is down | Critical only |

### 2.2 Response SLAs

| Severity | First Response | Triage Complete | Mitigation Target | Status Update Cadence |
|----------|---------------|----------------|-------------------|----------------------|
| Critical | < 15 min | < 30 min | < 1 h | Every 15 min |
| High | < 30 min | < 1 h | < 4 h | Every 30 min |
| Medium | < 4 h (business hours) | < 8 h | < 24 h | Daily |
| Low | < 1 business day | < 2 business days | Next release | Per sprint |
| Info | Next sprint planning | Triage at planning | Future sprint | Per sprint |

### 2.3 Escalation Tree

```
Critical / High incident detected
  │
  ├─► Tier 1 (on-call engineer)
  │     Respond within SLA
  │     Ack in PagerDuty or #ops-alert
  │     Create #ops-on-call-YYYYMMDD Slack channel
  │     Begin triage
  │     If unresolved after 15 min (Critical) / 30 min (High):
  │       │
  │       └─► Tier 2 (senior platform engineer / SRE lead)
  │             Join incident channel
  │             Coordinate remediation
  │             If unresolved after 30 min (Critical) / 2 h (High):
  │               │
  │               └─► Tier 3 (CTO / head of engineering)
  │                     Business-impact decisions
  │                     Customer comms approval
  │                     Infrastructure spend authorisation
  │
Medium / Low incident detected
  │
  ├─► Assigned engineer during business hours
  │     Verify severity
  │     Raise a Linear ticket (FAR team)
  │     Schedule fix per SLA
```

### 2.4 On-Call Responsibilities

- **Primary on-call:** First responder for all operational incidents. Carries
  the pager. Rotation: weekly, Mon 09:00 UTC.
- **Secondary on-call:** Backup if primary does not ack within 10 min. Same
  rotation, offset by 1 week.
- **Platform lead:** Tier 2 escalation. Not on pager rotation — available
  during business hours + call-out for Critical.
- **CTO / head of engineering:** Tier 3 escalation. Authorises
  business-continuity decisions, infrastructure spend, and customer
  notifications.

### 2.5 Out-of-Hours Protocol

| Severity | Action |
|----------|--------|
| Critical | Page primary on-call immediately. If no ack in 10 min, escalate to secondary. |
| High | Page primary on-call. If no ack in 20 min, escalate to secondary. |
| Medium | Log ticket. Assign next business day. |
| Low | Log ticket. Triage at next planning. |

### 2.6 When to Escalate to Security

If during triage you discover evidence of a security incident (active exploit,
data breach, unauthorised access), escalate to the security incident response
path defined in `docs/security/incident-response-playbook.md` §2.3.

---

## 3. Service Degradation / Outage Procedures

### 3.1 Detection Signals

| Signal | Possible Cause | Severity |
|--------|---------------|----------|
| `GET /health` returns non-200 or timeout | Backend crash-loop, DB unreachable, migration failure | Critical |
| p95 latency > 3x baseline for > 5 min | DB slow queries, Redis saturation, CPU/memory pressure | High |
| Error rate > 5% on any endpoint | Code defect, upstream dependency failure, config issue | High |
| HPA scaling to max replicas | Load spike, resource leak, insufficient capacity | Medium |
| Pod crash-loop or OOMKilled | Memory leak, bad config, missing secrets | Critical |

### 3.2 Quick Health Assessment

```bash
# Application health
curl -s https://modulo.example.com/api/v1/health | python3 -m json.tool

# Full health check (runs DB, Redis, migration checks)
uv run modulo health --full

# Container status
# (Docker Compose)
docker compose -f docker-compose.prod.yml ps

# Resource usage
docker stats
```

### 3.3 Graceful Degradation Strategy

| Degraded Component | Behaviour | User Impact |
|-------------------|-----------|-------------|
| **Database read-only** | API returns 503 for writes, read-only endpoints still work | Cannot create/edit pipelines, runs, or settings |
| **Redis unavailable** | Falls back to in-process scheduling and rate limiting | Single replica only — no horizontal scaling, no task durability |
| **Backend replica loss** (some containers down) | Remaining replicas serve traffic | Possible latency increase, no data loss |
| **LLM provider rate-limited** | Run enters retry loop with exponential backoff | Delayed pipeline completion |
| **Can't scale** (host resources exhausted) | Container start fails | Degraded throughput |

### 3.4 Recovery Procedures

**Backend crash-loop:**
1. Check logs: `docker compose -f docker-compose.prod.yml logs modulo --tail=100`
2. Check recent deployments: `git log --oneline -5`
3. If migration failure: check Alembic status and resolve per `docs/upgrade-process.md`
4. If missing secrets: verify the container environment — `docker compose -f docker-compose.prod.yml config | grep -i secret`
5. If resource exhaustion: increase memory limits or add replicas
6. Rollback if needed: restart with the previous image tag (see `docs/upgrade-process.md`)

**Complete outage (all instances down):**
1. Identify cause from container logs and events
2. If infrastructure failure (host down, volume lost), restore from backup per
   `docs/operations/backup.md` §Disaster Recovery Guide
3. If deployment defect, roll back per `docs/upgrade-process.md`
4. Restore database from backup if data corruption detected
5. Verify health with `uv run modulo health --full`

---

## 4. Database Failure Recovery

### 4.1 Detection

| Signal | Tool / Query | Possible Cause |
|--------|-------------|---------------|
| `pq: could not connect to server` | Backend logs | Postgres down, network issue, credentials rotated |
| `pq: remaining connection slots are reserved` | Backend logs | Connection pool exhausted |
| Slow queries > 500ms p95 | pg_stat_statements, OTel traces | Missing index, bad query plan, lock contention |
| Disk full | `df -h` on the Postgres host / volume | WAL accumulation, no retention policy |
| Replication lag > 30s | `pg_stat_replication` | Network latency, WAL shipping backlog |

### 4.2 Connection Pool Exhaustion

```bash
# Check active connections
docker compose -f docker-compose.prod.yml exec postgres psql -U modulo -c "
SELECT count(*) AS active_connections
FROM pg_stat_activity
WHERE state = 'active';
"

# Check max_connections
docker compose -f docker-compose.prod.yml exec postgres psql -U modulo -c "
SHOW max_connections;
"

# Kill idle connections (emergency)
docker compose -f docker-compose.prod.yml exec postgres psql -U modulo -c "
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle'
  AND state_change < NOW() - INTERVAL '30 minutes'
  AND backend_type = 'client backend';
"
```

**Resolution:**
1. Terminate idle connections (above) — immediate relief
2. Check for connection leaks in application code
3. Increase `max_connections` in Postgres config and restart
4. If persistent, increase connection pool size in backend config
5. If the pool is exhausted by long-running queries, use `pg_cancel_backend`
   for individual queries instead of `pg_terminate_backend` (less disruptive)

### 4.3 Disk Full

```bash
# Check disk usage (Postgres container)
docker compose -f docker-compose.prod.yml exec postgres df -h /var/lib/postgresql/data

# Find largest tables
docker compose -f docker-compose.prod.yml exec postgres psql -U modulo -c "
SELECT relname AS table,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 10;
"

# Check WAL retention
docker compose -f docker-compose.prod.yml exec postgres psql -U modulo -c "
SELECT COUNT(*) AS wal_files
FROM pg_ls_waldir();
"
```

**Resolution:**
1. **Immediate:** Extend the Postgres disk/volume (cloud volume resize or data migration per `docs/operations/backup.md`)
2. Clean up orphaned checkpoint data per `docs/operations/admin-bypass.md` §4.3
3. Archive old WAL files if WAL archiving is configured
4. Run `VACUUM ANALYZE` to reclaim dead tuples
5. If WAL accumulation is the cause, configure `wal_keep_size` or archive_timeout
6. Set up disk usage alerting at 80% capacity

### 4.4 Replication Failure

```bash
# Check replication status
docker compose -f docker-compose.prod.yml exec postgres psql -U modulo -c "
SELECT application_name,
       state,
       sync_state,
       pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()) AS replay_lag_bytes
FROM pg_stat_replication;
"
```

**Resolution:**
1. If replica is down, restart it and verify it catches up
2. If replication slot is broken, drop and recreate it
3. If lag > 30s and growing, check network bandwidth and WAL generation rate
4. If the primary is overloaded, offload read queries to the replica via
   connection routing
5. For read replica failover: promote the replica and update `DATABASE_URL`

### 4.5 Database Restore

Full restore procedures are in `docs/operations/backup.md` §Disaster Recovery
Guide. Quick reference:

```bash
# 1. Verify backup integrity
uv run scripts/restore.py --input /backups/daily/backup-YYYYMMDD.tar.gz.enc --dry-run

# 2. Stop the application
systemctl stop modulo   # or scale the service down via the orchestrator

# 3. Restore
uv run scripts/restore.py --input /backups/daily/backup-YYYYMMDD.tar.gz.enc --full

# 4. Restart and verify
systemctl start modulo   # or scale back up
uv run modulo health --full
```

---

## 5. Redis Failure Recovery

### 5.1 Detection

| Signal | Tool / Query | Possible Cause |
|--------|-------------|---------------|
| `Error 111 connecting to redis` | Backend logs | Redis down, network issue, credentials wrong |
| `OOM command not allowed` | Redis logs | `maxmemory` exceeded, no eviction policy |
| `LOADING Redis is loading the dataset` | Backend logs | Redis restarting, RDB/AOF loading |
| Evictions > 0 | `INFO stats` (evicted_keys) | Memory pressure, too-small maxmemory |

### 5.2 Graceful Degradation When Redis Is Down

Modulo degrades gracefully when Redis is unavailable:

| Feature | Without Redis | Data Loss Risk |
|---------|--------------|----------------|
| Task queue (SAQ) | In-process asyncio loop — tasks execute in the request process | Jobs scheduled during outage are lost if the process restarts |
| Rate limiting | In-memory no-op (rate limiting disabled) — allows all requests while Redis is down | No data loss |
| Cron scheduling | In-process asyncio loop | Duplicate triggers across replicas if > 1 replica running |
| Session cache | DB-backed sessions — slower but functional | No data loss |

**Single-replica deployments** (or when Redis is optional per
`docs/deployment.md`): The system continues to function with reduced
capabilities. Pipeline runs that are in-flight continue, but new SAQ-scheduled
jobs may not execute.

**Multi-replica deployments:** Redis is mandatory. Without it:
1. Both replicas fire cron triggers — duplicate pipeline executions
2. Rate limiting is per-process — effective rate cap doubles
3. Task queue is in-process — tasks scheduled on one replica disappear if that
   replica is terminated

### 5.3 Redis Recovery Procedure

```bash
# 1. Check Redis status
docker compose -f docker-compose.prod.yml exec redis redis-cli PING
docker compose -f docker-compose.prod.yml exec redis redis-cli INFO stats | grep -E "(evicted_keys|keyspace_hits|keyspace_misses)"

# 2. If Redis is OOM:
docker compose -f docker-compose.prod.yml exec redis redis-cli CONFIG SET maxmemory 512mb
docker compose -f docker-compose.prod.yml exec redis redis-cli CONFIG SET maxmemory-policy allkeys-lru

# 3. If Redis needs restart:
docker compose -f docker-compose.prod.yml restart redis

# 4. Verify Redis health
docker compose -f docker-compose.prod.yml ps redis
docker compose -f docker-compose.prod.yml exec redis redis-cli PING
```

### 5.4 Post-Recovery Steps

1. **Check for duplicate pipeline runs** after Redis reconnects —
   cron triggers may have fired during the outage if multiple replicas
   were running in degraded mode
2. **Verify rate limiting** is working across all replicas
3. **Verify SAQ worker** reconnection — check backend logs for
   `Connected to Redis` message
4. **Monitor memory** — if evictions were occurring, increase `maxmemory`

---

## 6. Queue Backpressure Handling

### 6.1 Queue Architecture

Modulo uses SAQ with Redis as the broker for the task queue, cron scheduling,
and rate limiting (SAQ replaced Celery in PR C of the Celery→SAQ migration,
ADR 017). When Redis is not configured, in-process asyncio loops
handle scheduling — no queue backpressure is possible in that mode since
jobs run inline.

### 6.2 Detection

| Signal | Tool | Severity |
|--------|------|----------|
| SAQ queue depth > 100 | SAQ web UI (system worker, 127.0.0.1:8081 via `fly ssh`) or Redis `LLEN saq:runs:queued` | Medium |
| Run start delay > 30s | Backend logs / OTel traces | High |
| Pipeline stuck in `queued` state > 5 min | API: `GET /runs/:id` | High |
| Worker `Received and deleted unknown message` | SAQ worker logs | Medium |

### 6.3 Inspect Queue Depth

SAQ stores each queue's jobs in Redis under `saq:<queue>:*` keys (queue names
derive from `SAQ_RUNS_QUEUE`, default `runs`; the system queue derives as
`system`). The ready-to-run backlog and in-flight jobs are Redis lists, the
total outstanding job set is a sorted set, and worker heartbeats are a sorted
set keyed by worker ID:

```bash
# Check SAQ queue depth directly in Redis
docker compose -f docker-compose.prod.yml exec redis redis-cli LLEN saq:runs:queued        # ready-to-run backlog (list)
docker compose -f docker-compose.prod.yml exec redis redis-cli LLEN saq:runs:active        # in-flight jobs (list)
docker compose -f docker-compose.prod.yml exec redis redis-cli ZCARD saq:runs:incomplete   # total outstanding incl. scheduled (zset)
docker compose -f docker-compose.prod.yml exec redis redis-cli ZCARD saq:runs:stats        # live runs-worker heartbeats (zset)

# Start a worker if none are running — runs worker:
docker compose -f docker-compose.prod.yml exec modulo python -m saq modulo.core.saq_worker.runs_settings
# System worker + web UI on 127.0.0.1:8081 (no --queue flag; the custom runner
# boots run_system_web, it does not print queue lengths):
docker compose -f docker-compose.prod.yml exec modulo python -m modulo.core.saq_worker
```

### 6.4 Backpressure Response

**Step 1 — Identify bottleneck:**
```bash
# Live workers per queue (SAQ heartbeats — one zset entry per running worker)
docker compose -f docker-compose.prod.yml exec redis redis-cli ZCARD saq:runs:stats
docker compose -f docker-compose.prod.yml exec redis redis-cli ZCARD saq:system:stats

# In-flight vs queued — workers are saturated when `active` is pinned near
# (live workers × SAQ_WORKER_CONCURRENCY, default 5) while `queued` keeps growing
docker compose -f docker-compose.prod.yml exec redis redis-cli LLEN saq:runs:active
docker compose -f docker-compose.prod.yml exec redis redis-cli LLEN saq:runs:queued
docker compose -f docker-compose.prod.yml exec redis redis-cli ZCARD saq:runs:incomplete
```

**Step 2 — Immediate mitigation:**
1. **Scale workers horizontally:**
   ```bash
   # Docker Compose: scale the SAQ worker services (docker-compose.yml)
   docker compose up -d --scale saq-runner=5 --scale saq-system=2
   # Fly.io: scale the SAQ `worker` process group (entrypoint.sh runs the workers
   # in-container), or raise per-worker concurrency
   ```
2. **Raise per-worker concurrency** if the backlog is caused by slow jobs rather
   than a flood — set `SAQ_WORKER_CONCURRENCY` (default 5, max 50) and restart
   the worker. SAQ has no Celery-style queue routing: the runs worker owns a
   single queue (`SAQ_RUNS_QUEUE`, default `runs`), so to isolate high-volume
   work, run a dedicated worker fleet on a separate queue name.
3. **Reject new work** if the queue is growing faster than workers can drain:
   - Rate-limit pipeline submission per user via `uv run modulo rate-limit set`
   - Temporarily disable non-critical triggers (polling, webhooks) in settings
   - If the spike is from a misconfigured trigger, disable it immediately

**Step 3 — Prevent recurrence:**
1. Review trigger configurations — a polling interval that is too aggressive
   can flood the queue
2. Set per-pipeline concurrency limits to prevent tenant-level DoS
3. Increase `SAQ_WORKER_CONCURRENCY` or add worker replicas/pools
4. Consider adding queue monitoring with alerts at depth thresholds (50, 100, 500)

### 6.5 Queue Recovery After Crash

If the SAQ worker or Redis crashes mid-job, jobs may be lost or stuck:

```bash
# 1. View in-flight jobs via SAQ admin UI (port 8081, system worker)
#    or check Redis directly (job IDs in the outstanding sorted set):
docker compose -f docker-compose.prod.yml exec redis redis-cli ZRANGE saq:runs:incomplete 0 -1

# 2. Jobs are auto-swept by SAQ sweeper; if stuck, restart the worker:
docker compose -f docker-compose.prod.yml restart modulo
```

---

## 7. Run Failure Surge / Throttling

### 7.1 Detection

| Signal | Tool | Severity |
|--------|------|----------|
| Run error rate > 10% | `modulo_pipeline_run_duration_seconds` + error status | Medium |
| Rapid consecutive failures on same pipeline | Backend logs | Medium |
| Unusual spike in pipeline submissions (> 5x normal) | API metrics | Medium |
| LLM provider returning 429/503 | Backend logs | Low (unless persistent) |

### 7.2 Immediate Throttling

```bash
# 1. Rate-limit pipeline submissions per org
uv run modulo rate-limit set --org-id <org-uuid> --limit 10/minute --key pipeline.submit

# 2. Freeze specific pipeline if it's causing failures
uv run modulo pipelines freeze <pipeline-id>

# 3. Block trigger execution for a problematic connector
uv run modulo connectors disable <connector-id>

# 4. If all pipelines are failing due to an upstream issue (e.g., LLM outage),
#    throttle globally
uv run modulo rate-limit set --global --limit 5/minute --key pipeline.submit
```

### 7.3 Run Failure Investigation

```bash
# 1. Check recent run failures
uv run modulo runs list --status failed --since 1h

# 2. Inspect a specific failed run
uv run modulo runs show <run-id>
uv run modulo runs logs <run-id>

# 3. Check for shared failure pattern (same node, same pipeline, same LLM)
uv run modulo runs list --status failed --since 1h --format json | python3 -c "
import json, sys, collections
runs = json.load(sys.stdin)
nodes = [r.get('failed_node') for r in runs if r.get('failed_node')]
print(collections.Counter(nodes).most_common(5))
"

# 4. Reset a stuck run (if not a code defect)
uv run modulo runs reset <run-id>
```

### 7.4 Run Surge Prevention

| Measure | Configuration | When to Apply |
|---------|--------------|---------------|
| Per-org pipeline rate limit | `RATE_LIMIT_PIPELINE_SUBMIT` | An org floods the queue |
| Per-pipeline concurrency limit | Pipeline settings | A single pipeline creates too many runs |
| Max concurrent sandbox runs per org | `Organisation.settings_json["sandbox_concurrency_limit"]` (via `GET`/`PUT /api/v1/admin/org/sandbox-concurrency`) | Default safeguard for sandbox-agent pipelines; `null` = unlimited |
| Global run rate limit | `uv run modulo rate-limit set --global` | Upstream LLM provider degradation |
| Webhook/trigger backoff | Connector settings | External system sends too many events |

---

## 8. Monitoring & Alerting Runbooks

### 8.1 Key Dashboards

| Dashboard | Access | What to Watch |
|-----------|--------|---------------|
| Pipeline performance | Grafana: `pipeline-performance.json` | Run durations, volumes, error rates |
| HITL review activity | Grafana: `hitl-review.json` | Gate activity, review speed, approval rates |
| Cost tracking | Grafana: `cost-tracking.json` | LLM spend by org/model/pipeline |
| Host & containers | Prometheus + Grafana (node_exporter + cAdvisor) | Node health, container resource usage |
| PostgreSQL | Grafana via postgres_exporter or pg_stat_statements | Active connections, query latency, replication lag |

### 8.2 Alert Rules

| Alert Name | Condition | Severity | Channel | Action |
|------------|-----------|----------|---------|--------|
| `BackendDown` | Probe failure > 3/3 on any backend instance | Critical | PagerDuty + #ops-alert | Follow §3 |
| `HighErrorRate` | HTTP 5xx > 5% of requests over 5 min | Critical | PagerDuty + #ops-alert | Follow §3 |
| `HighLatency` | p95 latency > 3s over 5 min | High | #ops-alert | Investigate slow endpoints |
| `QueueBacklog` | SAQ queue depth > 100 | High | #ops-alert | Follow §6 |
| `ConnectionPoolExhausted` | Active connections > 80% of max | High | #ops-alert | Follow §4.2 |
| `DiskUsageWarning` | Postgres disk > 80% | Medium | #ops-alert | Follow §4.3 |
| `RedisMemoryPressure` | Redis memory > 80% of maxmemory | Medium | #ops-alert | Follow §5 |
| `RunFailureSpike` | Run error rate > 10% over 5 min | Medium | #ops-alert | Follow §7 |
| `ContainerRestarts` | Backend container restarting repeatedly | Medium | #ops-alert | Follow §3.4 |
| `MigrationFailure` | Alembic upgrade failure on startup | Critical | PagerDuty + #ops-alert | Follow §3.4 |
| `CertificateExpiring` | TLS cert expires in < 14 days | Low | #ops-alert | Renew cert |

### 8.3 Creating Alert Rules

**Prometheus / Alertmanager:**

```yaml
# Example alert rule — add to your prometheus.yml rules file
groups:
  - name: modulo-operational
    rules:
      - alert: BackendDown
        expr: up{job="modulo-backend"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Backend instance {{ $labels.instance }} is down"
          runbook: "docs/operations/operational-incident-response.md#3"

      - alert: QueueBacklog
        expr: saq_queue_depth > 100
        for: 2m
        labels:
          severity: high
        annotations:
          summary: "SAQ queue depth is {{ $value }}"
          runbook: "docs/operations/operational-incident-response.md#6"
```

### 8.4 Health Endpoint Contract

The backend exposes `GET /health` and `GET /healthz`:

```json
{
  "status": "ok",
  "version": "1.0.0",
  "database": {
    "status": "ok",
    "pool_used": 3,
    "pool_max": 20
  },
  "redis": {
    "status": "ok",
    "connected": true
  },
  "migration": {
    "status": "ok",
    "current": "abc123def",
    "latest": "abc123def"
  }
}
```

A degraded component returns `"status": "degraded"` but the endpoint stays
HTTP 200. Only a full failure (DB unreachable, migration mismatch, Redis
connection refused) returns HTTP 503.

---

## 9. Escalation Contacts & Schedules

### 9.1 On-Call Schedule

| Role | Rotation | Coverage |
|------|----------|----------|
| Primary on-call | Weekly, Mon 09:00 UTC | 24/7 |
| Secondary on-call | Weekly, offset by 1 week | 24/7 (backup) |
| Platform lead (Tier 2) | Business hours + call-out | Mon-Fri 09:00-18:00 UTC |
| CTO (Tier 3) | By phone | 24/7 (Critical only) |

The on-call roster is maintained as a Linear issue in the farnalabs-modulo workspace
(FAR team).

### 9.2 Contact Methods

| Role | Primary | Secondary | Fallback |
|------|---------|-----------|----------|
| Primary on-call | PagerDuty push | Slack @mention | Phone call |
| Secondary on-call | Slack @mention | PagerDuty push | Phone call |
| Platform lead | Slack DM | Phone call | Email |
| CTO | Phone call | Slack DM | Signal |

### 9.3 Handover Procedure

1. The outgoing on-call documents any ongoing incidents in the `#ops-on-call`
   channel with status and next actions
2. The incoming on-call verifies they can access all dashboards and tools
3. PagerDuty rotation is automated — no manual handover needed for the
   notification path

---

## 10. Post-Incident Review Process

### 10.1 When a Post-Incident Review Is Required

| Severity | Review Required | Deadline | Reviewer |
|----------|----------------|----------|----------|
| Critical | Yes | 3 business days | Platform lead + CTO |
| High | Yes | 7 business days | Platform lead |
| Medium | Recommended | Next sprint | Assigned engineer |
| Low | Optional | Per team discretion | — |

### 10.2 Incident Timeline Format

```
| Time (UTC) | Event | Evidence |
|------------|-------|----------|
| 2026-06-30 14:00 | Alert triggered: backend p95 > 3s | PagerDuty incident #456 |
| 2026-06-30 14:02 | On-call acked | Slack #ops-alert |
| 2026-06-30 14:05 | Incident channel created | #ops-on-call-20260630 |
| 2026-06-30 14:10 | Triage: Postgres connection pool exhausted | pg_stat_activity output |
| 2026-06-30 14:12 | Mitigation: killed 40 idle connections | pg_terminate_backend |
| 2026-06-30 14:15 | Latency returned to baseline | Grafana dashboard |
| 2026-06-30 14:30 | RCA: connection leak in webhook handler | Git commit <sha> fixed |
| 2026-06-30 15:00 | Incident closed | Slack #ops-on-call-20260630 |
```

### 10.3 Root Cause Analysis

Structure the analysis around these questions:

- **What happened?** Narrative of the incident from detection to closure.
- **Why did it happen?** Technical root cause (code bug, config gap,
  infrastructure failure, capacity limit).
- **Why wasn't it caught earlier?** Detection gap — was the alert missing,
  too slow, or ignored?
- **What was the blast radius?** Actual vs. potential impact (users affected,
  data lost, downtime duration).
- **What went well?** Procedures that worked as intended.
- **What went wrong?** Gaps in detection, response, communication, or tooling.

### 10.4 Action Items

Each action item must include:

| Field | Required | Example |
|-------|----------|---------|
| ID | Yes | `PIR-2026-06-30-001` |
| Description | Yes | Add connection-pool-exhausted alert |
| Owner | Yes | @engineer-name |
| Severity | Yes | High |
| Due date | Yes | 2026-07-07 |
| Verification | Yes | Alert fires when connections > 80% in staging |
| Linked ticket | Recommended | Linear issue ID (FAR team) |

### 10.5 Lessons-Learned Integration

After the review is approved:

1. **Add to product map** — If the incident reveals an undocumented edge case
   or missing behaviour, add it to the relevant product map entry
2. **Update AGENTS.md** — If the incident pattern is likely to recur, add a
   prevention rule to the most specific AGENTS.md
3. **Update this playbook** — If the response procedure was missing a step,
   revise the relevant section
4. **Schedule a tabletop exercise** — For Critical incidents, schedule a
   tabletop exercise within 30 days to validate new controls
5. **Update monitoring** — If detection was delayed, add or adjust alert rules

### 10.6 Post-Incident Review Template

```markdown
# Post-Incident Review: <incident-id>

## Incident Summary

<3–5 sentence executive summary>

## Severity

<Critical | High | Medium>

## Timeline

| Time (UTC) | Event | Evidence |
|------------|-------|----------|

## Root Cause Analysis

### What happened?
### Why did it happen?
### Why wasn't it caught earlier?
### Blast radius
### What went well
### What went wrong

## Action Items

| ID | Description | Owner | Severity | Due | Verification |
|----|-------------|-------|----------|-----|-------------|

## Lessons Learned

<Paragraph on how this informs future development, testing, or operations.>

## Cross-References

- Incident channel: #ops-on-call-YYYYMMDD
- Related commits: <sha>
- Updated docs: <paths>
```

---

## 11. Preparedness Checklist

Run this checklist quarterly to ensure operational readiness:

- [ ] On-call rota is current and published
- [ ] PagerDuty integration tested with a simulated Critical alert
- [ ] All dashboard queries return data within expected ranges
- [ ] Backup integrity verified: `uv run scripts/restore.py --input <latest-backup> --dry-run`
- [ ] Redis memory and eviction rates reviewed
- [ ] Postgres disk usage < 80%
- [ ] All alert rules fire correctly in staging
- [ ] Tabletop exercise conducted for at least one operational scenario
- [ ] On-call handover procedure reviewed with current team
- [ ] Escalation contact list is up to date

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| Security incidents | `docs/security/incident-response-playbook.md` |
| Backup & restore | `docs/operations/backup.md` |
| Self-hosted admin operations | `docs/operations/self-hosted-admin.md` |
| Admin bypass (checkpoint) | `docs/operations/admin-bypass.md` |
| Performance baselines | `docs/operations/performance-baseline.md` |
| Deployment basics | `docs/deployment.md` |
| Deployment security | `docs/deployment-security.md` |
| Secret management | `docs/security/secret-management.md` |
| Network egress audit | `docs/operations/network-egress.md` |
| Product map (behaviour tracking) | `docs/product-map/` |
| Delivery plan / work items | Linear (farnalabs-modulo workspace, FAR team) |
