# QA Findings — Parameter Schema + Parameter Sets

## CRITICAL

### C1. `update_schema` double-increments version (version + 2 per update)
**File:** `backend/src/modulo/db/crud/parameter_schema.py:112-114`

```python
updates["version"] = ParameterSchema.version + 1  # SQL expression, not int!
apply_updates(schema, updates)                     # applies expression to instance
schema.version += 1                                # increments by 1 again
```

`ParameterSchema.version` is a SQLAlchemy `Mapped[int]` — accessing it on the class returns an `InstrumentedAttribute` (not an int). `ParameterSchema.version + 1` produces a `BinaryExpression` SQL expression (`parameter_schemas.version + 1`). When `apply_updates` calls `setattr(schema, "version", expr)`, SQLAlchemy stores the expression. Then `schema.version += 1` wraps it in another expression: `(version + 1) + 1`.

At flush time, the UPDATE renders as `SET version = version + 1 + 1` — version jumps by 2 on every save. If version starts at 1, after 3 saves it reads 7 instead of 4.

`update_set` in `parameter_set.py:93` correctly uses only `ps.version += 1` (no double-increment).

---

### C2. Optimistic locking is broken — concurrent updates silently overwrite each other
**File:** `backend/src/modulo/db/crud/parameter_schema.py:96-99` and `parameter_set.py:76-79`

Both `update_schema` and `update_set` use optimistic locking:
```python
result = await session.execute(
    select(ParameterSchema).where(
        ParameterSchema.id == schema_id,
        ParameterSchema.version == version,  # version check at READ time
    )
)
```

But the subsequent `flush()` issues `UPDATE ... WHERE id = :pk` with NO version check on the UPDATE. Two concurrent callers both read version=5, both pass the check, and both write. The second write silently overwrites the first. The version-increment only prevents infinite loops — it doesn't prevent lost updates.

The route handler's `409 CONFLICT` for `schema is None` (line 346-350) only fires if the SELECT returns no rows — i.e., the version was already changed by the time this transaction read it. But two callers reading the same version both get rows, and neither conflict fires.

**Fix:** Add `FOR UPDATE` to the SELECT, or add a `WHERE version = :read_version` to the UPDATE, or use PostgreSQL's `UPDATE ... RETURNING` pattern.

---

### C3. Frontend parameter sets always empty — flat array pagination mismatch
**Files:**
- `frontend/src/views/ParameterSchemasView.vue:882`
- `frontend/src/views/PipelineEditorView.vue:1037`

Backend endpoint `GET /api/v1/parameter-schemas/{schema_id}/sets` (`parameter_schemas.py:534`) has `response_model=list[SetResponse]` — it returns a flat **array** `[SetResponse, ...]`.

But the frontend reads:
```typescript
sets.value = (resp.data as any)?.items ?? []
```

Accessing `.items` on a flat array returns `undefined`, so `sets` is always `[]`. The sets tab shows "No parameter sets yet" even when sets exist. The set selector dropdowns in PipelineEditorView are always empty. This breaks the entire parameter set workflow on the frontend.

Same bug in `PipelineEditorView.vue:1037` (`loadParamSets`).

**Fix:** Change to `(resp.data as SetItem[]) ?? []`.

---

### C4. `schema_id` variable cross-contamination between iterations in graph validator
**File:** `backend/src/modulo/core/graph_validator/__init__.py:829-903`

In `_check_parameter_references`, the variable `schema_id` is defined inside the `if raw_schema_id is not None:` block at line 833, but referenced at line 879 outside any `raw_schema_id` guard:

```python
for node in nodes:
    raw_schema_id = node.get("parameter_schema_id")
    if raw_schema_id is not None:
        schema_id = try_parse_uuid(raw_schema_id)   # line 833 — defined here
        ...

    raw_set_id = node.get("parameter_set_id")
    if raw_set_id is not None:
        ...
        if raw_schema_id is not None:
            schema_id = try_parse_uuid(raw_schema_id)  # line 870
            ...

        if schema_id is not None:                    # line 879 — NO raw_schema_id guard!
            schema = schemas.get(schema_id)           # uses PREVIOUS node's schema_id!
```

If node A has `parameter_schema_id=X` and node B has only `parameter_set_id=Y` (no explicit `parameter_schema_id`), then at line 879 for node B, `schema_id` is still `X` from node A's iteration. Python's `for` loop doesn't create a new scope — the `if` block doesn't either. The drift warning compares a wrong schema's version against node B's set.

---

### C5. `get_set_references` is a stub — always returns empty
**File:** `backend/src/modulo/db/crud/parameter_set.py:110-114`

```python
async def get_set_references(...) -> dict[str, list[uuid.UUID]]:
    return {"pipeline_nodes": [], "snapshots": []}
```

The endpoint `GET /parameter-sets/{set_id}/references` always reports zero references. Deleting a set that's actively used in a pipeline appears to have no references. This is placeholder code that was never implemented.

---

## MAJOR

### M1. No unique constraint on `parameter_schemas(name)` — duplicate names allowed
**File:** `backend/src/modulo/db/migrations/versions/0014_parameter_schemas.py:48-60`

The migration creates `parameter_schemas` without a `UniqueConstraint("organisation_id", "name")`. The route handler (`parameter_schemas.py:229-234`) catches `IntegrityError` and reports "A parameter schema with this name already exists", but this exception can never fire for duplicate names — there's no constraint to violate it. The handler will only catch FK-related or PK-related IntegrityErrors, producing a misleading error message.

`parameter_sets` (migration line 83) correctly has `UniqueConstraint("parameter_schema_id", "name")`.

---

### M2. Misleading `IntegrityError` messages in GET endpoints
**File:** `backend/src/modulo/api/routes/parameter_schemas.py:178-181, 270-274, 367-370, 553-557, 653-656, 760-763`

Read-only endpoints (`GET /parameter-schemas/{id}`, `GET /parameter-schemas/{id}/sets`, `GET /parameter-schemas/{id}/sets/{id}`, `DELETE` endpoints) catch `IntegrityError` and return 409 "A resource with this value already exists". But read-only operations cannot trigger `IntegrityError` — this error handler is dead code. If it somehow fires, the message is misleading.

---

### M3. `list_schemas` cursor pagination has no ORDER BY
**File:** `backend/src/modulo/db/crud/parameter_schema.py:56-79`

When cursor pagination is used (line 62-79), the query `q` has no `.order_by()` clause. Cursor-based pagination requires deterministic ordering — without it, items may appear in different orders across requests, and `has_more` may be incorrect.

The non-cursor fallback (line 81) correctly uses `.order_by(ParameterSchema.created_at.desc())`.

---

### M4. Validation endpoint uses detached ORM object after transaction closes
**File:** `backend/src/modulo/api/routes/parameter_schemas.py:457-485`

The schema is loaded inside `async with session.begin():` at line 461, but `schema.parameters` is accessed outside the transaction at line 485:

```python
try:
    async with session.begin():
        ...
        schema = await get_schema(session, schema_id)
except ...:
    ...
# schema is now a detached ORM object
params = schema.parameters if isinstance(schema.parameters, list) else []
```

After the `session.begin()` block exits, the session is closed and `schema` is detached. For JSON columns (`parameters` is `Mapped[list[dict]]`), the data is loaded eagerly, so accessing `schema.parameters` works in practice. But accessing ANY uncached attribute would fail silently. If SQLAlchemy's loading strategy changes (e.g., deferred JSON columns), this breaks.

---

### M5. Error handler inconsistency — `/diff` endpoint has no try/except
**File:** `backend/src/modulo/api/routes/parameter_schemas.py:398-408`

```python
@router.get("/parameter-schemas/{schema_id}/diff")
@handle_db_errors("parameter_schemas.diff")
async def diff_parameter_schema_endpoint(...) -> dict[str, Any]:
    _log.warning("Diff endpoint not yet implemented for schema %s", schema_id)
    return {"from_version": from_version, "to_version": to_version, "changes": []}
```

This has no try/except block at all — relies entirely on `@handle_db_errors`. All other endpoints have local try/except blocks. If this endpoint starts doing DB work, errors will be caught at a less-specific level.

---

### M6. 650+ lines of duplicated error-handling boilerplate (DRY violation)
**File:** `backend/src/modulo/api/routes/parameter_schemas.py`

Every endpoint repeats the same 6-catch-block structure (`IntegrityError`, `ProgrammingError`, `SQLAlchemyError`, `HTTPException re-raise`, `Exception`) identically ~14 times. This is ~600 lines of nearly identical error handling. The `@handle_db_errors` decorator already exists and could be extended to handle the common patterns, removing the local try/except blocks.

---

### M7. Composite editor endpoint missing `SQLAlchemyError` handler
**File:** `backend/src/modulo/api/routes/composite_templates.py:316-346`

The `get_composite_editor_endpoint` catches `ProgrammingError`, `HTTPException`, and `Exception` — but NOT `SQLAlchemyError`. A transient DB failure would fall through to the generic `Exception` handler, producing a non-specific 500 instead of 503.

---

### M8. Frontend silent catch blocks in parameter loading
**Files:**
- `frontend/src/views/PipelineEditorView.vue:1038-1040` (`loadParamSets`)
- `frontend/src/views/PipelineEditorView.vue:1049-1051` (`loadParamSchemas`)

```typescript
} catch {
    // non-critical
}
```

Empty catch blocks swallow ALL errors without logging. A network failure, 500 error, or auth issue in parameter loading is invisible to developers and users. At minimum, `console.warn` should be used.

---

### M9. `/diff` endpoint is a permanent stub
**File:** `backend/src/modulo/api/routes/parameter_schemas.py:398-408`

The diff endpoint accepts `from_version` and `to_version` query parameters but always returns `{"changes": []}`. It's marked as not-yet-implemented. While acceptable as a TODO, it adds API surface that returns misleading data — callers get a 200 with empty changes and no indication the feature isn't built.

---

### M10. `parameter_bindings_json` in snapshot has no consistency guard
**File:** `backend/src/modulo/db/crud/pipeline_snapshot.py:79-133`

Two concurrent snapshot creations for the same pipeline can both read the same `max(snapshot_version)` = 5 and both create version 6. The pipeline row IS locked with `FOR UPDATE` (line 40), but the version query (line 158-163) uses `func.max(PipelineSnapshot.snapshot_version)` without `FOR UPDATE`. PostgreSQL forbids `FOR UPDATE` on aggregates, so the version increment is not atomic.

---

### M11. Migration downgrade may fail on FK constraint name
**File:** `backend/src/modulo/db/migrations/versions/0014_parameter_schemas.py:104-106`

```python
def _remove_agent_column() -> None:
    op.drop_constraint(op.f("fk_agents_parameter_schema_id"), "agents", type_="foreignkey")
    op.drop_column("agents", "parameter_schema_id")
```

The upgrade (line 92-101) adds the FK via `sa.ForeignKey(...)` without a named constraint — the DB auto-generates the name. The downgrade uses `op.f("fk_agents_parameter_schema_id")` which relies on Alembic's naming convention. If `env.py` doesn't have naming conventions configured, the auto-generated name won't match and the downgrade fails with "constraint does not exist".

---

## MINOR

### m1. Duplicate logger initialization
**File:** `backend/src/modulo/api/routes/parameter_schemas.py:35,37`

```python
logger = logging.getLogger(__name__)
_log = logging.getLogger(__name__)
```

Two references to the same logger. `_log` is used once (line 407, `/diff` endpoint). All other log statements use `logger`. Unnecessary duplication.

---

### m2. UTF-8 encoding corruption in migration docstring
**File:** `backend/src/modulo/db/migrations/versions/0014_parameter_schemas.py:3`

```
Implements RFC ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§10 Phase 1
```

The `§` character suffered repeated UTF-8-through-Latin-1 re-encoding. Should be `§10`.

---

### m3. Unnecessary `set_rls_user_context` calls in all route handlers
**File:** `backend/src/modulo/api/routes/parameter_schemas.py` (every endpoint)

Every endpoint calls `set_rls_user_context(session, principal.account_id, principal.org_role)` but the CRUD functions and RLS policies for `parameter_schemas` and `parameter_sets` tables only check `organisation_id` — they don't use user-level RLS. The call is harmless but adds unnecessary session-local writes on every request.

---

### m4. References endpoint returns agents as UUID-only objects
**File:** `backend/src/modulo/api/routes/parameter_schemas.py:443-445`

```python
return SchemaReferencesResponse(
    agents=[{"id": str(a)} for a in refs["agents"]],
    sets=[{"id": str(s)} for s in refs["sets"]],
)
```

The `get_schema_references` CRUD function selects only `Agent.id` (not `Agent.name`). The frontend `ReferenceResponse` interface has `name?: string`, but it's never populated. The references tab shows UUIDs twice instead of agent names.

---

### m5. `select` type validation uses `str(value)` comparison
**File:** `backend/src/modulo/api/routes/parameter_schemas.py:519`

```python
if options and str(value) not in options:
```

`str(value)` converts the value to string for comparison. This means `1` (int) and `"1"` (string) both match any option `"1"`. For numeric-looking select options, an int value that happens to stringify to an option passes validation. Minor in practice but technically incorrect.

---

### m6. `list_sets` has no pagination
**File:** `backend/src/modulo/db/crud/parameter_set.py:50-64`

Returns ALL sets matching the schema/org. No `limit`/`offset` or cursor support. For schemas with hundreds of sets, this returns the full list unconditionally.

---

### m7. `_check_parameter_references` re-parses `raw_schema_id` as UUID twice per node
**File:** `backend/src/modulo/core/graph_validator/__init__.py:833,870`

`try_parse_uuid(raw_schema_id)` is called once at line 833 and again at line 870 for the same node's data. The result is the same both times — minor inefficiency.

---

## Summary

| Severity | Count |
|---|---|
| CRITICAL | 5 |
| MAJOR | 11 |
| MINOR | 7 |

The most impactful issues:
1. **C1 + C2** — parameter schema updates are broken (version jumps by 2, concurrent updates conflict)
2. **C3** — the entire parameter sets tab is non-functional on the frontend (flat array vs paginated type mismatch)
3. **C4** — graph validator uses stale `schema_id` across iterations, producing wrong drift warnings
4. **M1** — no DB-level unique constraint on parameter schema names despite error messages claiming otherwise
