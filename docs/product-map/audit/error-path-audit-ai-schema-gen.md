# Error Path & Presentation Audit — feat-core-ai-schema-gen

## DOCUMENTED errors (product map match)

| # | Error path | Source | HTTP | In product map? | Tested? |
|---|---|---|---|---|---|
| E1 | Connector instance not found (infer) | `schemas.py:477` | 404 | Yes (line 53) | BDD scenario |
| E2 | Unsupported connector type (infer) | `schemas.py:489` | 400 | Yes (line 69 – BDD) | BDD scenario |
| E3 | No model backends configured (infer) | `schemas.py:496` | 400 | Yes (line 54) | BDD scenario |
| E4 | No model backends configured (generate) | `schemas.py:606` | 400 | Yes (line 54) | No BDD scenario* |
| E5 | DB ProgrammingError → 501 (infer) | `schemas.py:502` | 501 | Yes (line 93) | No specific BDD |
| E6 | DB ProgrammingError → 501 (generate) | `schemas.py:611` | 501 | Yes (line 94) | No specific BDD |
| E7 | Connector sampling generic exception → 502 | `schemas.py:526` | 502 | Yes (line 55: "502 when sampling fails") | No† |
| E8 | LLM inference failure → 502 | `schemas.py:540` | 502 | Yes (line 56) | Unit test |
| E9 | LLM generation failure → 502 | `schemas.py:631` | 502 | Yes (line 56) | Unit test |
| E10 | LLM timeout → SchemaInferenceError | `_common.py:43` | 502 (wrapped) | Yes (line 28) | Unit test (generation only) |
| E11 | Unparseable LLM response | `_common.py:57` | 502 (wrapped) | Yes (lines 29, 43) | Unit + integration |
| E12 | Non-string AIMessage.content | `_common.py:53` | 502 (wrapped) | Yes (lines 30, 44) | Unit + integration |
| E13 | Backend w/o `.content` attribute | `_common.py:50` | 502 (wrapped) | Yes (line 45) | Unit (generation only) |
| E14 | Empty/blank description (generate) | `generation.py:71` | ValueError → 500 | Yes (line 46) | Unit |
| E15 | Non-dict samples input (infer) | `inference.py:70` | ValueError → 500 | Yes (line 31) | No |

*\* BDD step `step_no_model_backends` only sets up mock for infer, not generate — tested via infer path only.*
*† ConnectorHub.sample() is mocked in BDD tests — no real sampling error path is tested.*

## UNDOCUMENTED errors (not in product map)

### Gap G1: Connector sampling timeout → 504 (missing from product map)
- **File:** `schemas.py:521-525`
- **Issue:** `TimeoutError` from `asyncio.timeout(30.0)` on connector sampling returns 504 Gateway Timeout, but product map line 55 only documents `502 when connector sampling fails`. The 504 path is completely undocumented.
- **Fix:** Add to product map: `POST /infer returns 504 when connector sampling times out`.

### Gap G2: ModelBackendHub empty `backend_ids` → StopIteration (uncaught)
- **File:** `schemas.py:534` (infer), `schemas.py:622` (generate)
- **Issue:** `next(iter(mh.backend_ids))` raises `StopIteration` if `backend_ids` is empty (e.g. backends deleted between list and initialise). Currently uncaught → raw 500.
- **Severity:** Low probability (race condition), but produces a cryptic 500 with no useful error message.
- **Fix:** Guard with `if not mh.backend_ids: raise HTTPException(503, "No model backends available")`.

### Gap G3: Audit event append failure destroys the response (infer)
- **File:** `schemas.py:551-565`
- **Issue:** `append_audit_event` runs AFTER the schema has been successfully inferred. If it fails (e.g. DB error), the exception propagates as a 500 and the user gets no schema back — despite the LLM call succeeding. The audit event is secondary.
- **Severity:** Medium — user retries, wastes another LLM call, gets billed twice.
- **Fix:** Wrap in `try/except log.exception(...)` — audit events should be best-effort, never fatal.

### Gap G4: ConnectorHub.initialise() uncaught exception
- **File:** `schemas.py:512`
- **Issue:** `await ch.initialise([ci])` is outside any try/except. If it raises (bad credentials, network error), the raw exception propagates as a 500.
- **Severity:** Medium — connector init failures are not impossible.
- **Fix:** Wrap in try/except → HTTPException with appropriate status.

### Gap G5: ModelBackendHub.initialise() uncaught exception
- **File:** `schemas.py:533` (infer), `schemas.py:621` (generate)
- **Issue:** Same as G4 — `await mh.initialise(...)` is outside any try/except.
- **Fix:** Wrap in try/except → HTTPException(502, "Model backend initialisation failed").

### Gap G6: StopIteration from empty backend_ids in generate endpoint
- **File:** `schemas.py:622`
- **Issue:** Same as G2, duplicated for generate endpoint.
- **Fix:** Same as G2.

## UNTESTED error paths

| # | Error path | Source | Unit test | Integration test | BDD |
|---|---|---|---|---|---|
| U1 | Connector sampling TimeoutError → 504 | `schemas.py:521` | — | — | — |
| U2 | Connector sampling generic Exception → 502 | `schemas.py:526` | — | — | — |
| U3 | DB ProgrammingError → 501 (infer) | `schemas.py:502` | — | — | — |
| U4 | DB ProgrammingError → 501 (generate) | `schemas.py:611` | — | — | — |
| U5 | LLM timeout → SchemaInferenceError (infer) | `_common.py:43` used from `inference.py` | — | — | — |
| U6 | Backend w/o `.content` (infer) | `_common.py:50` used from `inference.py` | — | — | — |
| U7 | Non-dict samples → ValueError | `inference.py:70` | — | — | — |
| U8 | Empty response after fence extraction (``` \n\n ```) | `_common.py:20-23` | — | — | — |
| U9 | ModelBackendHub empty backend_ids → StopIteration | `schemas.py:534,622` | — | — | — |
| U10 | No model backends configured (generate BDD) | `schemas.py:606` | — | — | — |

## Specific pattern findings

### `invoke_and_parse` — TimeoutError vs generic Exception (✓ correct)
- `TimeoutError` is caught at line 42 BEFORE generic `Exception` at line 45 via correct ordering. Timeouts get the specific "timed out" message; others get "LLM call failed". ✓

### `parse_schema_from_response` edge cases
- **Empty string:** `json.loads("")` → `JSONDecodeError` → caught upstream. Tested.
- **Fences with no content** (` ``` \n\n ``` `): After fence extraction, content is `""`, `json.loads("")` → `JSONDecodeError` → caught upstream. **NOT explicitly tested** — no test case for fences with only whitespace inside.
- **Malformed JSON:** Caught upstream. Tested.

### Timeout separation (✓ correct)
- Connector sampling = 30s (line 514)
- LLM inference = 60s (`_INFER_TIMEOUT` passed as `timeout` param)
- These are independent `asyncio.timeout()` contexts; no cross-contamination. ✓

### ProgrammingError → 501: BOTH endpoints covered (✓ correct)
- Infer: `schemas.py:502-507` ✓
- Generate: `schemas.py:611-616` ✓
- Both documented in product map lines 93-94. ✓

### Swallowing audit
- No bare `except:` blocks found
- No silent `return None` on failure found
- Exception chaining uses `from exc` / `from None` appropriately ✓

### Logging quality
- `_common.py:43` — `_log.error("Schema %s timed out after %ss", context, timeout)` — good, includes context and timeout value ✓
- `_common.py:46` — `_log.exception("LLM call failed during schema %s", context)` — `_log.exception` includes full traceback, good ✓
- `_common.py:59` — `_log.exception("Failed to parse %s schema from LLM response", context)` — good ✓
- `schemas.py:503` — `logger.exception("schemas.infer.table_missing")` — good ✓
- `schemas.py:612` — `logger.exception("schemas.generate")` — good ✓
- No secrets leaked into logs (response content is not logged) ✓

### User-facing message quality
- `404: "Connector instance not found"` — specific, includes entity ✓
- `400: "Connector type 'X' does not support schema inference. Supported types: A, B, C"` — very actionable ✓
- `400: "No model backends configured; cannot perform inference"` — actionable ✓
- `504: "Connector sampling timed out after 30s"` — specific ✓
- `502: "Failed to sample connector: {exc}"` — may leak internal exception details ✗
- `502: "Schema inference failed: {exc}"` — includes the `error_cls` message, which is sanitized ✓
- `501: "Schema inference is not available. Run database migrations to enable it."` — actionable ✓

### Message with exception leak: `schemas.py:529`
`f"Failed to sample connector: {exc}"` interpolates raw exception `__str__()` into the response. This is a minor information leak — connector implementation details could appear in the API response.

### Error message for `/import` (non-DB, but adjacent)
`"Invalid JSON: {exc}"` at line 863 also leaks the `json.JSONDecodeError` message. Pattern: `JSONDecodeError.msg` includes position info (`line 1 column 2 (char 1)`) — not secrets, but could expose file structure details.

## Summary fixes required

### Critical
None — no silent data loss or uncaught 500s for success paths.

### High
1. **G3** — Wrap `append_audit_event` in try/except to prevent audit failure from discarding successful inference results.
2. **G2/G6** — Guard `next(iter(mh.backend_ids))` against empty set → 503 with "No model backends available".

### Medium
3. **G4** — Wrap `ConnectorHub.initialise()` in try/except → HTTPException.
4. **G5** — Wrap `ModelBackendHub.initialise()` in try/except → HTTPException.
5. **U5** — Add unit test for LLM timeout in inference service (currently only tested for generation).
6. **U1/U2** — Add BDD scenarios for connector sampling timeout (504) and generic failure (502).
7. **U10** — Add BDD scenario for generate endpoint "no model backends" case.

### Low
8. **G1** — Update product map: add line for `POST /infer returns 504 when connector sampling times out`.
9. **U3/U4** — Add BDD scenarios for ProgrammingError → 501 on infer/generate endpoints.
10. **U7** — Add unit test for non-dict samples → ValueError.
11. **U8** — Add test for fences with only whitespace.
12. **U6** — Add unit test for backend w/o `.content` for inference (generation tested).
13. `schemas.py:529` — Consider sanitizing exception message before including in 502 response.
