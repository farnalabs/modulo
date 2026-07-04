# Cross-Module Contract Audit: `feat-core-ai-schema-gen`

**Date:** 2026-07-04
**Worktree:** `ai-schema-gen-147`
**Scope:** Interface contracts between `ai-schema-gen` and its dependencies (`ConnectorHub`, `ModelBackendHub`, `_common.py`, `feat-core-schema-inference`)

---

## Dependency Graph

```
ai-schema-gen ──depends-on──▶ feat-core-schema-inference
       │                            │
       │  uses ConnectorHub         │  defines SchemaInferenceService
       │  uses ModelBackendHub      │  defines SchemaGenerationService
       │  uses _common.py           │  uses _common.py
       ▼                            ▼
  backend/src/modulo/api/routes/schemas.py
  backend/src/modulo/core/schema_registry/inference.py
  backend/src/modulo/core/schema_registry/generation.py
```

---

## Finding C-1 [CRITICAL] — `ConnectorHub.initialise()` silently skips failing connectors, leading to misleading 502

**Location:** `schemas.py:511-530` calling `ch.initialise([ci])` then `ch.sample(...)`

**The contract:**
- `ConnectorHub.initialise()` (connector_hub/__init__.py:125-163) has a `try/except Exception` around each connector's decryption + construction. If decryption fails (`ConnectorDecryptError`) or the builder raises (`ValueError` for missing config), the exception is caught, logged as a warning, and the connector is **silently omitted** from `_connectors`.
- `ConnectorHub.sample()` calls `_lookup()` which raises `ConnectorNotFoundError` when the ID isn't registered.

**The bug:** When a connector instance fails to initialise (bad credentials, corrupted ciphertext, missing config), `initialise()` silently skips it. Then `sample()` raises `ConnectorNotFoundError`. The endpoint catches it as a generic `Exception` and returns:

```python
except Exception as exc:
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Failed to sample connector: {exc}",
    ) from exc
```

This is **misleading** — the connector didn't "fail to sample", it failed to initialise (credentials/config problem). The user sees a 502 when the real error is a configuration problem, not a transient network failure.

**Fix:** Catch `ConnectorNotFoundError` from `ch.sample()` explicitly and return 404 "Connector instance not initialised" or 502 with a clear message about failed initialisation. Better: make `initialise()` raise/report the failure rather than silently skipping, or catch `ConnectorNotFoundError` in the endpoint and return a 400-level error.

---

## Finding C-2 [CRITICAL] — `ModelBackendHub.initialise()` exceptions not caught in infer/generate endpoints

**Location:** `schemas.py:532-533` (infer) and `schemas.py:620-621` (generate)

**The contract:**
- `ModelBackendHub.initialise()` (model_backend_hub/__init__.py:103-120) can raise:
  - `BackendDecryptError` — when `secrets_backend.get_secret()` raises `KeyError` (missing secret)
  - `ValueError` — from `_build_backend()` for missing credential keys or unknown providers
  - `KeyError` — from `_get_cred()` if a required credential key is absent

**The bug:** Neither endpoint catches exceptions from `mh.initialise()`. If a model backend's credentials are missing from the secrets backend, or the ciphertext is corrupted, the raw exception propagates as a **500 Internal Server Error**:

```python
async with ModelBackendHub() as mh:
    await mh.initialise(mbs.items, secrets_backend=secrets_backend)  # ← unhandled: BackendDecryptError, ValueError
    first_backend_id = next(iter(mh.backend_ids))
    backend = await mh.get(first_backend_id)  # ← unhandled: BackendUnavailableError
```

Compare with `ConnectorHub.initialise()` which internally catches exceptions per-connector (line 157) — `ModelBackendHub.initialise()` does NOT. This is an **inconsistency** between the two hubs' error-handling contracts. If any backend in the list fails decryption, the entire request fails.

**Fix:** Wrap `mh.initialise()` in a try/except catching (`BackendDecryptError`, `ValueError`) → return 502 with a clear message. Or make `ModelBackendHub.initialise()` internally resilient per-backend, matching `ConnectorHub`'s pattern.

---

## Finding C-3 [CRITICAL] — `mh.get()` can raise `BackendUnavailableError` — not caught

**Location:** `schemas.py:535` (infer) and `schemas.py:623` (generate)

**The contract:** `ModelBackendHub.get()` (model_backend_hub/__init__.py:122-154) raises `BackendUnavailableError` when no backend (primary or fallback) is healthy.

**The bug:** After `mh.get(first_backend_id)` succeeds in returning a backend reference, `backend.invoke(...)` is called inside `SchemaInferenceService.infer()` / `SchemaGenerationService.generate()`. But within `invoke_and_parse` (`_common.py:39-47`), the `except Exception as exc` catch wraps even transient connection errors (rate limits, network blips, auth failures) as a generic `SchemaInferenceError("LLM call failed")`. The error type is lost — the user can't distinguish "quota exceeded" from "no healthy backend" from "LLM returned invalid JSON".

However, the bigger issue: **`BackendUnavailableError` itself is not caught** at the endpoint level. If a race condition marks the backend unhealthy between `mh.initialise()` and `mh.get()`, a 500 error propagates instead of a 502.

**Fix:** Wrap `mh.get()` in a try/except catching `BackendUnavailableError` → return 502 "No healthy model backend available".

---

## Finding C-4 [CRITICAL] — Generate endpoint has no audit event

**Location:** `schemas.py:637` (generate endpoint exits without audit)

**The contract:** The infer endpoint writes an audit event `schema_inference_completed` (lines 552-565). The generate endpoint does not write any audit event.

**The bug:** Schema generation is a user-facing AI action with compliance and observability implications, just like inference. Without an audit event, there's no record of:
- Who generated a schema and when
- Which model backend was used
- The description/examples used as input

**Fix:** Add `append_audit_event()` with event_type `schema_generation_completed` in the generate endpoint, mirroring the infer pattern. Consider adding it as a `finally` block or after `service.generate()` succeeds.

---

## Finding M-1 [MAJOR] — `_SUPPORTED_INFERENCE_TYPES` is disconnected from ConnectorHub; missing git-host types

**Location:** `schemas.py:485-488`

**Current list:**
```python
_SUPPORTED_INFERENCE_TYPES = frozenset({
    "github", "gitlab", "jira", "linear", "slack",
    "notion", "confluence",
})
```

**Issues:**
1. **Not derived from a shared source** — this list is a local constant in the endpoint. The `ConnectorHub._build_connector()` has ~40 connector types. If a new git-host or document-store connector is added to ConnectorHub, there's no automated mechanism to update this list.
2. **Missing git-host types** — `bitbucket`, `azure_repos`, `gitea` are valid git-host connectors supported by ConnectorHub with `query()` capability, but are excluded from inference.
3. **"Slack" is out-of-scope per PRD** — the PRD (8.16) specifies inference for **issue-tracker**, **git-host**, and **document-store** connectors only. Slack is a chat/messaging platform and doesn't fit any of these categories.

**Fix:** Move the authoritative list to ConnectorHub (e.g., a classmethod or property `ConnectorHub.supported_inference_types`) and reference it from the endpoint. Or check the connector type against a PRD-defined mapping rather than a hardcoded flat set. Remove "slack" or document why it's included.

---

## Finding M-2 [MAJOR] — Error message leaks internal details via `f"Failed to sample connector: {exc}"`

**Location:** `schemas.py:526-530`

**The bug:** The generic `Exception` catch in the infer endpoint passes `exc` directly into the HTTP response detail:

```python
except Exception as exc:
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Failed to sample connector: {exc}",
    ) from exc
```

If `exc` is a `ConnectorNotFoundError`, the detail becomes `"Failed to sample connector: <connector_uuid>"` — leaking the internal UUID. If `exc` is a `ValueError` from `_build_connector()` (e.g., missing config), the message could leak configuration requirements.

**Fix:** Log the full exception with `logger.exception()`, return a generic message (`"Failed to sample connector"`) without `{exc}`. Use different status codes for different error types (e.g., 404 for not-found, 502 for transient errors).

---

## Finding M-3 [MAJOR] — `_common.py:invoke_and_parse` generic Exception handler loses error type information

**Location:** `_common.py:45-47`

**The bug:** The catch-all `Exception` from `backend.invoke()` wraps ALL failures as `error_cls("LLM call failed")`:

```python
except Exception as exc:
    _log.exception("LLM call failed during schema %s", context)
    raise error_cls("LLM call failed") from exc
```

This flattens distinct failure modes (429 rate limit, 401 auth failure, connection timeout, API internal error) into a single generic message. The user sees "Schema inference failed: LLM call failed" with no way to distinguish "you hit your rate limit" from "your API key is invalid" from "the LLM service is down".

**Fix:** Consider surfacing the error type or adding metadata to the exception. At minimum, ensure the HTTP response includes a helpful message based on the exception type. Adding rate-limit specific handling (429) would also improve UX.

---

## Finding m-1 [MINOR] — `next(iter(mh.backend_ids))` could raise `StopIteration`

**Location:** `schemas.py:534` (infer) and `schemas.py:622` (generate)

The `mbs.items` check at lines 497-501 / 606-610 validates that at least one model backend exists before entering this block. However, if `ModelBackendHub.initialise()` throws internally and partially populates `_backends`, or if internal state is inconsistent, `next(iter(mh.backend_ids))` would raise `StopIteration` → 500.

This is protected but tightly coupled — the guard and the usage are separated by an `except ProgrammingError` block and a `create_secrets_backend()` call. A safer pattern would be to keep the backend close by using it directly rather than re-deriving from `backend_ids`.

---

## Finding m-2 [MINOR] — `ConnectorDecryptError` silently skipped, surfaces as opaque 502

**Location:** `connector_hub/__init__.py:138` and `schemas.py:526-530`

When `initialise()` catches `json.JSONDecodeError` and wraps it as `ConnectorDecryptError` (inside the inner try block at line 135-138), the inner `except json.JSONDecodeError` is outside the outer `except Exception` on line 157? Let me re-read the nesting:

```python
# line 132-163
for ci in instances:
    try:
        raw_str = await self._secrets_backend.get_secret(str(ci.id))
        try:
            creds: dict[str, Any] = json.loads(raw_str)
        except json.JSONDecodeError as exc:
            raise ConnectorDecryptError(ci.id) from exc
        connector = _build_connector(...)
        ...
        self._connectors[ci.id] = traced
    except Exception as exc:
        logger.warning("Skipping connector %s (%s): %s", ci.id, ci.connector_type_id, exc)
```

So `ConnectorDecryptError` IS caught by the outer `except Exception` — it's logged as a warning and skipped. The connector is silently omitted. Then the user gets 502 "Failed to sample connector: ..." without any indication that credential decryption was the root cause.

---

## Summary Table

| ID | Severity | Description | File:Line |
|---|---|---|---|
| C-1 | Critical | `ConnectorHub.initialise()` silently skips failing connectors → `ConnectorNotFoundError` → misleading 502 | schemas.py:511-530 |
| C-2 | Critical | `ModelBackendHub.initialise()` exceptions (`BackendDecryptError`, `ValueError`) not caught → 500 | schemas.py:532-533, 620-621 |
| C-3 | Critical | `mh.get()` `BackendUnavailableError` not caught → 500 | schemas.py:535, 623 |
| C-4 | Critical | Generate endpoint has no audit event (asymmetry with infer) | schemas.py:637 |
| M-1 | Major | `_SUPPORTED_INFERENCE_TYPES` hardcoded, disconnected from ConnectorHub, missing bitbucket/azure_repos/gitea, includes slack | schemas.py:485-488 |
| M-2 | Major | `f"Failed to sample connector: {exc}"` leaks internal details in HTTP response | schemas.py:529 |
| M-3 | Major | `invoke_and_parse` generic `Exception` handler flattens distinct LLM errors into "LLM call failed" | _common.py:45-47 |
| m-1 | Minor | `next(iter(mh.backend_ids))` could raise `StopIteration` | schemas.py:534, 622 |
| m-2 | Minor | `ConnectorDecryptError` silently skipped by `initialise()`, no clear error path to user | connector_hub/__init__.py:138 |

## Interface Health Assessment

**Common module (`_common.py`):** Clean contract. `invoke_and_parse()` provides a consistent error boundary for both inference and generation. The only concern is the loss of error-type information (M-3).

**Service layer (`inference.py`, `generation.py`):** Clean contracts. Both accept `ModelBackendBase`, produce dict, raise typed errors. No interface drift between the two.

**API endpoints (`schemas.py`):** The contract between endpoints and hubs has gaps. `ModelBackendHub` error states are not all handled. `ConnectorHub`'s silent-skip pattern creates a confusing error path.

**Connector types:** `_SUPPORTED_INFERENCE_TYPES` needs to be reconciled with ConnectorHub's actual capabilities. Consider moving this to ConnectorHub as an authoritative method.
