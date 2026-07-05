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

- `str(mapped_output.get(field, ""))` when the field value can be `None` → check for None explicitly: `raw = mapped_output.get(field); value = "" if raw is None else str(raw)`. `str(None)` produces the literal string `"None"`, which passes regex patterns like `r".*"` and `r"^None$"` — masking a missing/null field as valid data.

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

- When accepting a user-supplied callable and parsing its return value with `.get()`, wrap the parsing in `try/except TypeError` or validate `isinstance(ret, dict)` first. A non-dict return (e.g. `None`, a list, a string) crashes the caller with `AttributeError: 'NoneType' object has no attribute 'get'`.

### Alembic / Entrypoint

- **Deployed databases may have orphaned `alembic_version` entries from restructured migration branches.** When a branch migration is rebased onto the main chain, the old revision ID stays in the DB's `alembic_version` table. The entrypoint (`deploy/fly/entrypoint.sh`) runs `cleanup_orphan_migrations.py` before `alembic upgrade heads` to remove any `version_num` that doesn't match a known migration file. If the remaining chain diverges (because the DB migrated through old branch revisions whose schema changes are already present), the entrypoint falls back to `alembic stamp head`. This logic lives in `deploy/fly/cleanup_orphan_migrations.py` and must be kept in sync with the actual migration files — it reads `revision:` from every `.py` in `migrations/versions/`.

- **Alembic `env.py` sync URL driver must use a package in production deps.** The `_to_sync_url` function converts `postgresql+asyncpg://` to `postgresql+psycopg2://`. But `psycopg2` is not in the production dependency tree — only `psycopg-binary` (psycopg v3) is. Use `postgresql+psycopg://` (psycopg v3) instead of `postgresql+psycopg2://`. The fix is in `backend/src/modulo/db/migrations/env.py:_to_sync_url`. Without this, Docker builds fail at startup with `ModuleNotFoundError: No module named 'psycopg2'`.
