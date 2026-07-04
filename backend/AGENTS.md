# Backend — Agent Guidance

## Lessons Learned

- Loop variable over module import name (`for status in ...` when `from fastapi import status`) → rename the import with an alias (`import status as http_status`). A loop variable shadows the module for its entire scope, so any reference like `status.HTTP_500_...` after the loop raises `AttributeError`. This is especially dangerous in exception handlers — the handler itself crashes trying to reference the shadowed module.

- SQL aggregate functions (`SUM`, `COUNT`, `func.sum()`, etc.) return `None` when the result set is empty, not `0`. Always wrap `int(result.scalar_one())` in a null-safe helper like `_safe_int` that returns a default for `None`. Without this, `int(None)` raises `TypeError` which is NOT caught by `except SQLAlchemyError:`.

- Every API endpoint that runs database queries needs BOTH `except SQLAlchemyError:` (for SQL failures) AND `except Exception:` (for Python-level errors like `TypeError`, `AttributeError`, `ValueError` from data processing). Without the generic catch, non-SQL errors propagate to the CatchAllMiddleware and produce an opaque 500 with no structured detail.

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
