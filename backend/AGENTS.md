# Backend — Agent Guidance

## Lessons Learned

### SQL: raw f-strings are SQL injection

- `text(f"SELECT ... WHERE id = '{value}'")` creates SQL injection vectors even for internal use. Always use parameterized queries: `text("SELECT ... WHERE id = :val").bindparams(val=value)` or SQLAlchemy ORM expressions. This was the single most common critical finding across codebase QA — files across all layers (CRUD, routes, aggregations, analysis) used interpolated values in SQL text.

### TOCTOU: check-then-act requires atomicity

- Reading a value (e.g. "is this slot available?") then acting on it (e.g. "assign to slot") in separate queries creates a race window where another request can interleave. Use `SELECT ... FOR UPDATE` (Postgres row lock) or a single atomic `UPDATE ... WHERE ... RETURNING` to eliminate the window. Found in slot assignment, budget enforcement, and duplicate-prevention logic.

### Cross-tenant: every multi-tenant query must include `organisation_id = :org_id`

- Missing org scoping was found in audit routes, dashboard aggregations, and notification queries — not just entity CRUD. When adding a new query, grep for `organisation_id` in the WHERE clause as a pre-merge check. On non-Postgres backends (MariaDB, SQLite), the `_inject_tenant_filter` listener handles this automatically, but raw `text()` queries bypass it entirely.

### Rollback: `session.rollback()` destroys in-progress data from other operations

- Prefer `savepoint = await session.begin_nested()` for local rollback scopes. The outer `session.rollback()` discards ALL uncommitted writes, including those from other concurrent operations on the same session — not just the failed one.

### Python 3.13: `Mapped["Type | None"]` forward reference syntax is broken

- Python 3.13 changed PEP 604 union parsing in annotations. `Mapped["Type | None"]` raises `TypeError` at class body execution. Use `Mapped[Optional["Type"]]` or `Mapped[Union["Type", None]]` instead.

### Test login: payload must use `email` field, not `username`

- The login endpoint validates against `OAuth2PasswordRequestForm` which expects `{"email": "...", "password": "..."}` — not `{"username": "...", "password": "..."}`. Sending `username` produces a silent 422 validation error, causing all subsequent test assertions to fail with confusing messages. This was found in 3 separate test files across BDD, unit, and integration tests.

### Event loops: `asyncio.new_event_loop()` must be closed

- Test fixtures that create a new event loop must close it in `finally` or `addfinalizer`. Unclosed loops accumulate and eventually cause `RuntimeError: Event loop is closed` on unrelated async tests. BDD test files had 80+ instances of unclosed event loops.

### WebSocket test fixtures: always close the connection

- `async with client.websocket_connect("/ws") as ws:` must be wrapped in `try/finally ws.close()` — without an explicit close, the test hangs at teardown because the WS connection is never released.

### Sensitive data: auth tokens must never appear in logs

- Several test fixtures and a load-test scenario logged `Authorization: Bearer <token>` or API response bodies containing secrets. Use `logging.getLogger(...).setLevel(logging.WARNING)` on noisy loggers, or strip sensitive fields before logging. Also: never log raw API request/response bodies in production code — they may contain credentials.

- Loop variable over module import name (`for status in ...` when `from fastapi import status`) → rename the import with an alias (`import status as http_status`). A loop variable shadows the module for its entire scope, so any reference like `status.HTTP_500_...` after the loop raises `AttributeError`. This is especially dangerous in exception handlers — the handler itself crashes trying to reference the shadowed module.

- SQL aggregate functions (`SUM`, `COUNT`, `func.sum()`, etc.) return `None` when the result set is empty, not `0`. Always wrap `int(result.scalar_one())` in a null-safe helper like `_safe_int` that returns a default for `None`. Without this, `int(None)` raises `TypeError` which is NOT caught by `except SQLAlchemyError:`.

- Every API endpoint that runs database queries needs BOTH `except SQLAlchemyError:` (for SQL failures) AND `except Exception:` (for Python-level errors like `TypeError`, `AttributeError`, `ValueError` from data processing). Without the generic catch, non-SQL errors propagate to the CatchAllMiddleware and produce an opaque 500 with no structured detail.

- Every `except SQLAlchemyError:` and `except Exception:` handler must log the exception with `_log.exception()` before returning the error response. Without logging, a SQLAlchemyError (like a trigger function crashing on a non-UUID column) produces an opaque 503 with no traceback, making root-cause investigation impossible. The pattern is:
  ```python
  except SQLAlchemyError:
      _log.exception("agents.create_agent")  # <-- always log
      raise HTTPException(status_code=503, detail="Database temporarily unavailable.")
  ```

- `model_validate()` error handlers must never use bare `raise` — always raise a structured `HTTPException` instead. A bare `raise` inside an `except Exception` block propagates the original exception to the CatchAllMiddleware, producing an opaque 500 with no structured detail. Pattern: `except Exception: raise HTTPException(status_code=500, detail="...") from None`.

- PATCH endpoint `model_dump(exclude_none=True)` → use `exclude_unset=True`. `exclude_none=True` prevents clearing nullable fields because keys with `None` values are omitted from the dump, so setting a field to `None` becomes a no-op instead of a NULL update.

- Cross-field validation (e.g. `visibility='team' requires owner_team_id`) must be added to BOTH the `Create` model and the `Update` model — it's common to add it only to Create and forget Update.

- When calling a service function with keyword arguments, the caller's argument names must match the function's parameter names. A mismatch (`account_id=...` when the parameter is `created_by`) raises `TypeError` at runtime. Verify both call site and definition when renaming parameters.

- Content-Disposition `filename=` values must be sanitized to alphanumeric + limited special chars (`-_.`). Simple `replace()`-based sanitization leaves HTTP header injection vectors via `"`, `\`, `;`, or `\r\n`. Use `"".join(c if c.isalnum() or c in "-_." else "_" for c in name)`.

- Upload file size validation should check `file.size` (Content-Length header) before calling `await file.read()` to reject oversized uploads without allocating memory. Also re-check `len(data)` after read as a safety net for chunked requests without Content-Length.

- Analysis functions that stamp metadata keys onto a mutable dict argument (e.g. `_analyse_bundle` setting `_resolved_id` on bundle entries) must `copy.deepcopy()` the dict first. Otherwise, a transaction rollback leaves stale metadata in the caller's dict object, corrupting subsequent retries or reuses of the same reference.

- BDD feature file API paths must match the actual router prefix. A feature file referencing `/api/composite-templates` when the router uses `/api/v1/composite-templates` causes silent false passes (or 404s in production). Always cross-reference the `prefix=` argument in the route file.

- Frontend ParameterPort interface fields must mirror the backend Pydantic ParameterPort model. When adding a field to one side (e.g. `multiline`, or changing `options` type), update the other side in the same delivery. A type mismatch between `str[]` (backend) and `{value, label}[]` (frontend) causes runtime rendering errors for select inputs.

- `except Exception` for external library calls (JMESPath, regex, etc.) → narrow to the specific exception type the library documents (e.g. `jmespath.exceptions.JMESPathError`, `re.error`). Bare `except Exception` masks programming bugs like `TypeError` from wrong argument types.

- Failure routing by `startswith(f"'{name}'")` → add a trailing delimiter (`startswith(f"'{name}':")`) so that a short name like `"a"` does not also route failures for `"ab"`.

- Integer fields that must be non-negative (retry counts, pages, sizes) → always add `Field(ge=0)`. Without it, negative values pass Pydantic validation and cause logic errors (e.g. `retry_count (0) >= -1` → immediate exhaustion).

- PUT endpoints that accept only a subset of a JSON blob (e.g. `{nodes, edges}` from a graph editor) → merge with the existing blob, don't replace entirely. `db_obj.field | update_dict` preserves unmanaged metadata keys (viewport, zoom, comments) that would otherwise be silently deleted on every save.

- Unit tests for standalone modules (`modulo.core.*`, `modulo.auth.*`, etc.) should import the module under test directly instead of going through `modulo.api.main`. Importing `modulo.api.main` at module level triggers MCP server startup and database connection pooling, causing the test suite to hang indefinitely. Prefer `from modulo.core.license import parse_and_verify` over `from modulo.api.main import app` in pure unit tests.

- Base64 padding for `urlsafe_b64decode` — always compute proper padding with `'=' * (-len(b64) % 4)`. Python < 3.13 rejects unpadded input, and assuming exactly 2 chars of padding fails when the input is already padded or has a different length.

- Module-level mutable lists (e.g. `_KNOWN_FLAGS`) must not be assigned directly as default instance attributes — use `list(source)` to create an independent copy per instance. Direct assignment shares the same list object across all instances, so mutations in one instance affect all others.

- `unittest.mock.patch.stop()` returns `None` — never call `.stop().__aexit__()`. In `__aexit__` handlers, use `p.stop()` directly. Calling `p.stop().__aexit__(...)` calls `None.__aexit__()` which silently fails, leaking patches.

- `os.environ.pop()` / `os.environ[key]=val` in tests without `try/finally` → use `monkeypatch.delenv()` / `monkeypatch.setenv()`. `monkeypatch` automatically restores the original value on teardown, preventing cascading failures when a test mutates global env state.

- `pytest.raises(Exception)` in tests → narrow to the specific exception type the code is expected to raise (e.g. `ValueError`, `JWTError`). Bare `Exception` masks bugs where the code raises an unexpected exception (including `SystemExit`, `KeyboardInterrupt`) and the test passes.

- `output.get(field, output)` when field is absent from the dict silently validates the entire dataset instead of the intended sub-field. If a specific field is requested (non-empty `field`), check `field in output` explicitly and fail with a clear error message — don't fall back to the parent dict.

- Config values that are expected to be a `dict` (e.g. a callable registry) must be validated with `isinstance(val, dict)` before accessing `.get()` or `[]`. A non-empty list is truthy, so `config.get("key") or {}` does NOT fall back to `{}` for list values — `[1, 2].get(key)` raises `AttributeError`.

### HTTP: every `requests` call must have an explicit timeout

- Even in scripts, tools, and test helpers, every `requests.Session.post()` / `.get()` / `.request()` call must pass `timeout=N`. Without it, a network hang blocks the caller indefinitely. This applies to `_login()` methods that bypass a shared `_request()` helper — they must set `timeout=` independently.

### HTTP response handling: call `raise_for_status()` before JSON parsing

- Pattern `if not resp.ok: log_error(); resp.raise_for_status(); return resp.json()` creates a dead branch: `raise_for_status()` always raises on non-2xx, so the `if not resp.ok` branch is either redundant (never reached) or the only guard. Simplify to: `resp.raise_for_status()` first (covers all non-2xx), then `resp.json()`. This gives a single, clear control flow with no dead branches.

### URL construction: use `urlparse`/`urlunparse`, not string replace chains

- Building URLs with `.replace("http://", "").replace("https://", "").replace("/api/v1", "").rstrip("/")` is fragile — it breaks on unexpected URL formats (query params, auth, fragments) and silently corrupts edge cases. Use `urllib.parse.urlparse` + `urlunparse` to manipulate scheme, netloc, and path separately.

- When accepting a user-supplied callable and parsing its return value with `.get()`, wrap the parsing in `try/except TypeError` or validate `isinstance(ret, dict)` first. A non-dict return (e.g. `None`, a list, a string) crashes the caller with `AttributeError: 'NoneType' object has no attribute 'get'`.

### Alembic / Entrypoint

- **Deployed databases may have orphaned `alembic_version` entries from restructured migration branches.** When a branch migration is rebased onto the main chain, the old revision ID stays in the DB's `alembic_version` table. The entrypoint (`deploy/fly/entrypoint.sh`) runs `cleanup_orphan_migrations.py` before `alembic upgrade heads` to remove any `version_num` that doesn't match a known migration file. If the remaining chain diverges (because the DB migrated through old branch revisions whose schema changes are already present), the entrypoint falls back to `alembic stamp head`. This logic lives in `deploy/fly/cleanup_orphan_migrations.py` and must be kept in sync with the actual migration files — it reads `revision:` from every `.py` in `migrations/versions/`.

- **Alembic `env.py` sync URL driver must use a package in production deps.** The `_to_sync_url` function converts `postgresql+asyncpg://` to `postgresql+psycopg2://`. But `psycopg2` is not in the production dependency tree — only `psycopg-binary` (psycopg v3) is. Use `postgresql+psycopg://` (psycopg v3) instead of `postgresql+psycopg2://`. The fix is in `backend/src/modulo/db/migrations/env.py:_to_sync_url`. Without this, Docker builds fail at startup with `ModuleNotFoundError: No module named 'psycopg2'`.

### Connectors: shell command construction with env vars

- When prepending env vars to a shell command in `_build_exec_cmd`, env vars must prefix the final command itself (`KEY=VALUE cmd`), not appear as a separate `&&`-delimited statement (`KEY=VALUE && cmd` which is invalid shell).

### Connectors: pagination cursor must not double as resource identifier

- Never use `q.filters.get("id") or q.cursor` as a fallback resource ID. The pagination cursor is a bookmark, not an entity identifier. Mixing them means a caller that passes a cursor gets a wrong/failed API call instead of a clear validation error.

### Connectors: import stdlib modules at module level

- Always import stdlib modules (`datetime`, `base64`, `uuid`, `urllib.parse`) at module level, not inside method bodies. Lazy imports inside methods: (1) pay the import cost on every invocation, (2) delay dependency-failure detection from import time to first-use time, (3) are flagged by linters. Found in 4 connector files.

### Connectors: use `key in dict` for required filter validation, not `dict.get(key)` with falsy check

- `if not value` after `dict.get(key)` rejects falsy-but-valid values (empty string `""`, integer `0`, boolean `False`). For required fields validation, use `if key not in filters` / `if key not in data` instead. This matches the GitLab connector's correct pattern and avoids introducing subtle bugs when a valid field value is falsy.

### FastAPI router ordering: include specific routes before catch-all path-param routers

- When a router with a path parameter (e.g. `errors_router` with `/{error_id}` where `error_id` is a UUID) is included BEFORE a router with a more specific path (e.g. `error_forwarder_config_router` with `/forwarders`), the catch-all router matches first — it tries to parse `"forwarders"` as a UUID and fails with a 422 validation error. Always include routers with specific, non-parameterized paths before routers with path parameters. This applies to `include_router()` ordering in `main.py`.

### Response model serialization: never return raw ORM objects from route handlers

- FastAPI route handlers that return a `response_model` must pass Pydantic-model-converted objects, not raw SQLAlchemy ORM instances. Returning a raw ORM object causes FastAPI's response serialization to fail with a 500 Internal Server Error because ORM instances don't match the Pydantic response schema structure. Always wrap ORM results: `return [SsoProviderResponse.from_orm(p) for p in providers]` or use `model_validate()`.

### AsyncSession must always pass `autobegin=False`

The DI factory in `dependencies.py:93` creates sessions with `autobegin=False`. Any code that manually constructs an `AsyncSession` (e.g. `AsyncSession(engine)` or `AsyncSession(session.bind)`) MUST pass `autobegin=False` to match.

With the default `autobegin=True`, `Session.commit()` auto-starts a new implicit transaction. The next `async with session.begin():` then raises `InvalidRequestError: A transaction is already begun on this Session.` because the implicit transaction is already active. This is enforced by semgrep rule `async-session-missing-autobegin`.

Found in `remy.py`: the `event_generator` created `AsyncSession(session.bind)` without `autobegin=False`, causing every Remy streaming request to fail with "Database error. Please try again later."

### Redis is required for production deployments

Modulo assumes Redis is present in production. The startup sequence in `main.py` hard-errors if `REDIS_URL` is not set and `settings.debug` is false. All three Fly tiers set `REDIS_URL = ""` by default — they MUST be provisioned with Upstash Redis before deploying:

```powershell
fly redis create --name modulo-app-redis -r lhr,ams --enable-eviction
```

Then set `REDIS_URL` in the corresponding `fly.*.toml` to the connection string from `fly redis status <name>`.

In-memory fallbacks exist at many call sites (rate limiter `core/rate_limiter.py`, dashboard cache `api/routes/dashboard.py`, error tracking keys `core/error_tracking/__init__.py`, alert cooldowns `alerting.py`, EventBus `event_bus.py`, WS tokens `auth.py:315`, Celery scheduler `celery_app.py`) — these are acceptable in debug mode but silently lose state in production on deploy or scale-up. The eventual goal is to remove all fallbacks and hard-require Redis.

### Model backends: `health_check` overrides must re-raise `asyncio.CancelledError`

- Backends that override `health_check()` with a broad `except Exception` must add `except asyncio.CancelledError: raise` before the generic catch, matching the base class pattern in `base.py:84-85`. Without it, cancellation during shutdown is silently suppressed on Python < 3.12 (where CancelledError inherits from Exception).

### Model backends: local/Ollama-compatible backends must set `supports_tools = True`

- Backends that wrap `ChatOpenAI` (Ollama, Jan, llama.cpp, LM Studio, LocalAI, TGI, vLLM) inherit `supports_tools = False` from `ModelBackendBase`. Since `ChatOpenAI` supports tool calling, the subclass must explicitly set `supports_tools: bool = True` — otherwise tool routing is silently disabled.

### Model backends: health checks must not pass API keys in URL query parameters

- Health check requests that pass API keys as URL query parameters (e.g. `params={"key": self._api_key}`) expose the credential in server logs, proxy logs, and error messages via `str(exc)`. Always use header-based auth (`Authorization: Bearer`). If a provider requires query-param auth, sanitize the URL before logging.

### Model backends: use module-level constants for base URLs

- When a backend references its API base URL in both `__init__` and `health_check`, define it as a module-level constant (`COHERE_BASE_URL = "..."`) rather than hardcoding the string twice. This prevents drift between the two usages.

### Async init guards: use double-checked locking, not a bare boolean

- Setting `self._initialised = True` at the end of an async `initialise()` method WITHOUT a lock creates a race window. Two concurrent coroutines can both pass the `if self._initialised: return` guard (the check is between the flag being False and being set), then interleave their state mutations into `self._connectors`. Fix: use `asyncio.Lock()` with a double-checked locking pattern — check the flag outside the lock for fast-path return, then re-check inside the lock before the write path. Found in `ConnectorHub.initialise()` at `backend/src/modulo/core/connector_hub/__init__.py`.

The Remy in-memory event registries (`_pending_ui_results`, `_pending_permissions`, `_session_approvals` in `remy.py:93-97`) have NO fallback at all — they are process-local `asyncio.Event` objects. Any deploy restart destroys in-flight Remy conversations. A Redis pub/sub replacement for these registries is the highest-priority follow-up.

### `set_rls_org` must be called inside `session.begin()`

- `set_rls_org(session, org_id)` calls `_ensure_active_transaction()` which raises `RuntimeError` if there is no active transaction. With `session.autobegin=False` (the DI default), calling `set_rls_org` before `async with session.begin():` will always crash. Always place `set_rls_org` inside the `async with session.begin():` block, never before it.

### PostgreSQL trigger functions casting non-UUID columns crash on VARCHAR values

- `enforce_same_organisation()` trigger function used `(to_jsonb(NEW) ->> TG_ARGV[1])::uuid` which casts EVERY column value to UUID, regardless of column type. For VARCHAR columns like `agents.input_schema_version` (value `"latest"`), this raises `invalid input syntax for type uuid` which surfaces as a 503 (`SQLAlchemyError`). Always check `information_schema.columns.data_type` before casting in a trigger function:
  ```sql
  SELECT data_type INTO col_type FROM information_schema.columns
  WHERE table_name = TG_TABLE_NAME AND column_name = TG_ARGV[1];
  IF col_type IS DISTINCT FROM 'uuid' THEN
    RETURN NEW;
  END IF;
  ```

### MCP `auth_principal` fields must be consistent across all auth paths

- The MCP auth middleware sets `request.scope["auth_principal"]` with different field sets depending on the auth path (API key, OAuth, JWT). Missing fields in one path (e.g. `user_id` missing from JWT path) cause `KeyError` in downstream middleware like `RateLimitMiddleware._client_key()`. When adding a field to `auth_principal` in any auth path, verify all other auth paths set the same field — or use `.get()` with defaults in consumers.

### MCP Starlette sub-apps need custom exception handlers for visibility

- `Starlette` adds `ServerErrorMiddleware` automatically, which catches unhandled exceptions and returns a plain text `"Internal Server Error"` with **no logging**. When building a Starlette sub-app (like the MCP server), always add `exception_handlers={Exception: handler}` with `_log.exception()` so production errors are visible in logs instead of silently swallowed.

### Module docstring must precede `from __future__ import annotations` (E402)

- Placing the module docstring AFTER `from __future__ import annotations` causes the triple-quoted string to be treated as a bare expression statement (not a docstring), triggering ruff E402 on ALL subsequent imports ("module-level import not at top of file"). The fix is always: docstring → `from __future__ import annotations` → other imports. This was the single most common finding across the QA sweep (~200+ occurrences in error_tracking, pipeline_engine, connectors, model_backends, otel_bridge, secrets_backend, and many more modules).

### Tests using `require_feature` routers must override `get_plan_context`

- When a FastAPI route uses `dependencies=[require_feature("error_forwarders")]` (or any feature name), the `require_feature` dependency runs before the route handler. If the test mocks a DB query to produce e.g. `ProgrammingError`, the mock returns `None` for feature checks, causing a 402 `FEATURE_REQUIRED` instead of the expected 501/503. Fix: override `get_plan_context` in the test app's `dependency_overrides` to return a `PlanContext` that enables all features:
  ```python
  class _AllFeatures:
      def feature_enabled(self, name: str) -> bool: return True
      def list_enabled_features(self) -> list: return []
      def tier(self) -> str: return "enterprise"
      def has_license_key(self) -> bool: return True
  async def _override_plan_context() -> _AllFeatures:
      return _AllFeatures()
  app.dependency_overrides[get_plan_context] = _override_plan_context
  ```

### Health check API keys must use headers, not URL query parameters

- When a backend passes its API key as a URL query parameter (`?key=...` or `params={"key": self._api_key}`), the credential is exposed in server logs, proxy logs, and error messages via `str(exc)`. Always use header-based auth (`Authorization: Bearer` or provider-specific header like `x-goog-api-key`). Found in the Gemini model backend during R2 QA.

### Docker on Windows: NTFS junctions are not followed in build context

- Docker for Windows does NOT follow NTFS junctions/reparse points when resolving files in a build context. If you create a junction at `backend/frontend/ → ../frontend`, the Docker build (`build: ./backend`) will not see `frontend/src/manifest.yaml` through the junction — COPY fails with "not found". Fix: either (a) remove the COPY from the Dockerfile and rely on runtime volume mount (the compose file already has `./frontend/src/manifest.yaml:/app/manifest.yaml`), or (b) change the build context to the repo root and adjust COPY paths accordingly.

### Module-level raises for optional deps block the entire application

- Never `raise ImportError(...)` at module level for an optional dependency. A module-level raise prevents the module from loading, which cascades up to crash the entire uvicorn process (or any caller that imports the module). Instead, use a graceful fallback pattern: catch `ImportError`, set a boolean flag (e.g. `CELERY_AVAILABLE = False`), and replace the imported class with a stub (`_CeleryTask = object`). Guard the optional-class definition behind the flag, and let consumers check `CELERY_AVAILABLE` at call time. Found in `webhook_dedup_cleanup.py` where `from celery import Task` raised at import time, blocking uvicorn startup.

### SQL: `FOR UPDATE` is not allowed with aggregate functions

- `SELECT max(run_number) ... FOR UPDATE` raises `asyncpg.exceptions.FeatureNotSupportedError` — PostgreSQL explicitly forbids `FOR UPDATE` on aggregate queries. `FOR UPDATE` locks rows, but aggregates operate on the result set as a whole. Only use `FOR UPDATE` on queries that select individual rows (e.g. `SELECT ... FROM pipelines WHERE id = :pid FOR UPDATE`). Found in `db/crud/run.py:create_run()` where the `SELECT max(run_number)` for the next run number had a dangling `.with_for_update()`. The error surfaced as a generic 503 ("Database temporarily unavailable") because:
  1. The aggregate `FOR UPDATE` raised `FeatureNotSupportedError` (subclass of `SQLAlchemyError`)
  2. Route handlers and MCP tools caught `except SQLAlchemyError` / `except Exception` and returned opaque error messages
  3. The `_log.exception()` output was invisible in Fly logs (JSON-structured logging format not rendered by `fly logs`)

### Exception handlers must log the full traceback — generic catch blocks hide root causes

- Every `except SQLAlchemyError:` and `except Exception:` handler that returns a generic error message (503, internal_error) MUST call `_log.exception()` with the full traceback BEFORE returning. Without logging, the actual error (e.g. `FeatureNotSupportedError: FOR UPDATE is not allowed with aggregate functions`) is lost behind an opaque "Database temporarily unavailable." After deploying with detailed error messaging (`print(f"ERROR: {e}\n{traceback.format_exc()}", flush=True)`), the real cause was visible. Without it, the error looked like a connection pool issue for hours.

- `_log.exception()` output using the structured `JsonFormatter` may NOT appear in `fly logs` output — Fly's log shipper doesn't reliably render JSON-formatted log lines. For guaranteed visibility during debugging, use `print(f"ERROR: ...", flush=True)` to stderr or stdout. Remove debug prints before merging to release.

### MCP `trigger_pipeline` creates run records but does NOT execute them

- The MCP `trigger_pipeline` tool creates a `Run` record with `status="pending"` and returns immediately. The run stays `pending` forever because the MCP tool doesn't start LangGraph execution. Only the REST API route (`POST /api/v1/runs`) calls `background_tasks.add_task(_run_in_background, executor, ...)` which actually runs the pipeline. The MCP tool should either start execution itself or clearly document that the run requires a separate execution step. As of July 2026, use the REST API to trigger executable runs.

### Route auth: admin-only routes must use `get_current_user`, not `get_current_tenant_user`

- When a route checks admin permissions internally (via `_require_admin` or `is_system_admin`), use `Depends(get_current_user)` for the auth dependency — NOT `Depends(get_current_tenant_user)`. The tenant user dependency requires `organisation_id` and `org_role` to be non-None, which system admins may not have (they can be admin without org membership). Using `get_current_tenant_user` causes a 403 "Organisation membership required" before the admin check even runs. The `_require_admin` guard is the sole gate needed for system-admin routes. Found in the feature-flag org-override endpoints.
