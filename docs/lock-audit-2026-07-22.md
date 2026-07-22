# Lock Point Audit — 2026-07-22

## Summary

Audited 54 `with_for_update()` call sites across `backend/src/modulo/`. Classified into three categories: **CRITICAL** (blocks pipeline execution / concurrent webhook dispatch), **NORMAL** (entity CRUD serialisation), and **SAFE** (money/auth/token operations needing strict ordering).

## Findings Table

| File | Line | Entity Locked | Category | Recommendation |
|---|---|---|---|---|
| `core/trigger_engine/__init__.py` | 395 | Trigger (replay) | **CRITICAL** | Replace with `pg_try_advisory_lock` — concurrent replays for same trigger serialise all webhook traffic |
| `core/trigger_engine/__init__.py` | 705 | Trigger (_load_trigger) | **CRITICAL** | Replace with `pg_try_advisory_lock` — called by `handle_webhook`; serialises all concurrent webhook requests for the same trigger |
| `core/cron_scheduler.py` | 205 | Trigger (fire_cron_trigger) | **CRITICAL** | Replace with `pg_try_advisory_lock` — serialises cron-trigger fires for the same trigger |
| `core/trigger_engine/polling.py` | 207 | Trigger (fire_polling_trigger) | **CRITICAL** | Replace with `pg_try_advisory_lock` — serialises polling-trigger fires for the same trigger |
| `api/mcp_server.py` | 701 | Pipeline | CRITICAL | Keep FOR UPDATE — MCP pipeline operations need serialisation (concurrent MCP requests) |
| `core/pipeline_engine/executor.py` | 226 | Pipeline | CRITICAL | Keep FOR UPDATE — pipeline execution state machine requires strict ordering |
| `core/pipeline_engine/executor.py` | 249 | Pipeline | CRITICAL | Keep FOR UPDATE — pipeline execution state machine requires strict ordering |
| `core/pipeline_engine/recovery.py` | 97 | Pipeline | CRITICAL | Keep FOR UPDATE — recovery must atomically claim the pipeline |
| `db/crud/run.py` | 78 | Run | CRITICAL | Keep FOR UPDATE — run state transitions need atomicity |
| `db/crud/run.py` | 176 | Run | CRITICAL | Keep FOR UPDATE — run state transitions need atomicity |
| `db/crud/run.py` | 202 | Run | CRITICAL | Keep FOR UPDATE — run state transitions need atomicity |
| `api/routes/pipelines.py` | 1377 | Pipeline | CRITICAL | Keep FOR UPDATE — route handler serialising pipeline mutations |
| `api/routes/pipelines.py` | 1487 | Pipeline | CRITICAL | Keep FOR UPDATE — route handler serialising pipeline mutations |
| `core/hitl_manager/__init__.py` | 223 | HitlClaim | CRITICAL | Keep FOR UPDATE — HITL claim/release must be atomic |
| `auth/oauth.py` | 247 | OAuthAuthorizationCode | SAFE | Keep FOR UPDATE — OAuth code exchange must be atomic |
| `auth/oauth.py` | 384 | OAuthAuthorizationCode | SAFE | Keep FOR UPDATE — OAuth code exchange must be atomic |
| `db/crud/token_family.py` | 18 | TokenFamily | SAFE | Keep FOR UPDATE — token generation needs ordering |
| `db/crud/token_family.py` | 59 | TokenFamily | SAFE | Keep FOR UPDATE — token generation needs ordering |
| `db/crud/token_family.py` | 83 | TokenFamily | SAFE | Keep FOR UPDATE — token generation needs ordering |
| `db/crud/agent.py` | 136 | Agent | NORMAL | Keep FOR UPDATE — agent CRUD serialisation |
| `db/crud/agent.py` | 203 | Agent | NORMAL | Keep FOR UPDATE — agent CRUD serialisation |
| `db/crud/pipeline.py` | 90 | Pipeline | NORMAL | Keep FOR UPDATE — pipeline CRUD serialisation |
| `db/crud/pipeline.py` | 296 | Pipeline | NORMAL | Keep FOR UPDATE — pipeline CRUD serialisation |
| `db/crud/pipeline_snapshot.py` | 40 | Pipeline | NORMAL | Keep FOR UPDATE — snapshot creation |
| `db/crud/pipeline_snapshot_versioning.py` | 125 | Pipeline | NORMAL | Keep FOR UPDATE — snapshot versioning |
| `db/crud/variant_group.py` | 120 | VariantGroup | NORMAL | Keep FOR UPDATE — variant group CRUD |
| `db/crud/variant_group.py` | 182 | VariantGroup | NORMAL | Keep FOR UPDATE — variant group CRUD |
| `db/crud/parameter_set.py` | 83 | ParameterSet | NORMAL | Keep FOR UPDATE — parameter set CRUD |
| `db/crud/parameter_schema.py` | 102 | ParameterSchema | NORMAL | Keep FOR UPDATE — parameter schema CRUD |
| `db/crud/org_deletion.py` | 123 | Organisation | NORMAL | Keep FOR UPDATE — org deletion needs atomicity |
| `db/crud/org_deletion.py` | 165 | Organisation | NORMAL | Keep FOR UPDATE — org deletion needs atomicity |
| `db/crud/org_deletion.py` | 199 | Organisation | NORMAL | Keep FOR UPDATE — org deletion needs atomicity |
| `db/crud/org_deletion.py` | 221 | Organisation | NORMAL | Keep FOR UPDATE — org deletion needs atomicity |
| `db/crud/sso_provider.py` | 65 | SsoProvider | NORMAL | Keep FOR UPDATE — SSO provider CRUD |
| `db/crud/sso_provider.py` | 139 | SsoProvider | NORMAL | Keep FOR UPDATE — SSO provider CRUD |
| `db/crud/observability.py` | 23 | Organisation | NORMAL | Keep FOR UPDATE — observability settings |
| `db/crud/system_config.py` | 23 | SystemConfig | NORMAL | Keep FOR UPDATE — system config needs atomic reads |
| `db/crud/error_tracking.py` | 78 | ErrorGroup | NORMAL | Keep FOR UPDATE — error group updates |
| `db/crud/daily_run_count.py` | 42 | DailyRunCount | NORMAL | Keep FOR UPDATE — usage counting |
| `db/crud/node_observation.py` | 35 | NodeObservation | NORMAL | Keep FOR UPDATE — observation recording |
| `db/crud/node_composite.py` | 27 | Node | NORMAL | Keep FOR UPDATE — node composite operations |
| `db/crud/node_composite.py` | 33 | Node | NORMAL | Keep FOR UPDATE — node composite operations |
| `core/workflow_import_export/__init__.py` | 86 | Pipeline | NORMAL | Keep FOR UPDATE — export serialisation |
| `core/cost_controller/__init__.py` | 55 | CostBudget | SAFE | Keep FOR UPDATE — cost tracking needs ordering |
| `core/cost_controller/__init__.py` | 122 | CostBudget | SAFE | Keep FOR UPDATE — cost tracking needs ordering |
| `core/cost_controller/__init__.py` | 143 | CostBudget | SAFE | Keep FOR UPDATE — cost tracking needs ordering |
| `core/secrets_backend/fernet.py` | 166 | EncryptedSecret | NORMAL | Keep FOR UPDATE — secret rotation |
| `core/audit_logger/__init__.py` | 244 | AuditChainHead | SAFE | Keep FOR UPDATE — audit trail integrity |
| `core/runtime_provider/hub.py` | 162 | WorkspaceLease | NORMAL | Keep FOR UPDATE — workspace lease management |
| `core/reports/scheduler.py` | 215 | ReportSchedule | NORMAL | Keep FOR UPDATE — report scheduling |
| `api/routes/model_backends.py` | 255 | ModelBackend | NORMAL | Keep FOR UPDATE — model backend configuration |
| `core/remy/context_source_service.py` | 129 | ContextSource | NORMAL | Keep FOR UPDATE — context source config |
| `core/remy/context_source_service.py` | 139 | ContextSource | NORMAL | Keep FOR UPDATE — context source config |
| `core/mcp_setup_handoff/__init__.py` | 93 | Pipeline | NORMAL | Keep FOR UPDATE — MCP setup handoff |

## Changes Applied

The 4 **CRITICAL** trigger-related FOR UPDATE locks were replaced with Postgres advisory locks (`pg_try_advisory_lock`):

| File | Line (old) | Old Pattern | New Pattern |
|---|---|---|---|
| `core/trigger_engine/__init__.py` | 395, 705 | `SELECT ... FOR UPDATE` on Trigger | `pg_try_advisory_lock(trigger_id)` + load without FOR UPDATE |
| `core/cron_scheduler.py` | 205 | `SELECT ... FOR UPDATE` on Trigger | `pg_try_advisory_lock(trigger_id)` + load without FOR UPDATE |
| `core/trigger_engine/polling.py` | 207 | `SELECT ... FOR UPDATE` on Trigger | `pg_try_advisory_lock(trigger_id)` + load without FOR UPDATE |

All other FOR UPDATE locks are retained — they protect row-level state machines (run state, pipeline execution, token generation, audit chain integrity) that genuinely need strict serialisation.
