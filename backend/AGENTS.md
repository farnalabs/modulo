# Backend — Agent Guidance

## Lessons Learned

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
