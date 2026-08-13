---
id: feat-core-determination
prd: 8.16
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/determination.py
  - backend/src/modulo/determination/draft.py
  - backend/src/modulo/determination/inference.py
  - backend/src/modulo/determination/scanner.py
bdd:
  - backend/tests/bdd/features/determination/determination.feature
unit-tests:
  - backend/tests/unit/determination/test_draft.py
  - backend/tests/unit/determination/test_inference.py
  - backend/tests/unit/determination/test_scanner.py
  - backend/tests/unit/api/test_determination_endpoint.py
depends-on: [feat-connectors-hub, feat-connectors-github, feat-connectors-gitlab, feat-connectors-jira, feat-connectors-linear]
status: partial
---

# SDLC Assessment and Pipeline Draft Generation

The determination endpoint scans a team's connected tools (GitHub, GitLab, Jira, Linear) to infer SDLC maturity and generate editable pipeline drafts.

## Behaviours

- [x] GET /api/v1/determination — scan + infer SDLC maturity
- [x] POST /api/v1/determination/draft — scan + generate editable pipeline draft
- [x] Integration with GitHub/GitLab connectors for code activity analysis
- [x] Integration with Jira/Linear connectors for issue tracking analysis
- [x] Pipeline draft generation from inferred workflow
- [x] Both routes require operator role (403 for non-operators) via `require_permission("determination.scan")`
- [x] Endpoint-level unit tests exist for both route handlers (`test_determination_endpoint.py`, 15 tests)
- [x] BDD coverage exists (`determination.feature`, 4 scenarios — operator scan, operator draft, viewer 403, empty scan)

## Error Handling

- [x] Both routes catch `ProgrammingError` → 501 with migration hint
- [x] Both routes catch `SQLAlchemyError` → 503
- [x] Both routes catch `IntegrityError` → 409
- [x] Both routes catch `HTTPException` → re-raise
- [x] Both routes catch `Exception` → 500 with `logger.exception`
- [x] `ConnectorDecryptError` during hub initialisation → 502 Bad Gateway
- [ ] No connector-specific error handling — connector failures (rate limits, auth expiry) propagate as 500
- [ ] No rate limiting on expensive scan/draft endpoints

## Edge Cases

- [x] No connectors configured returns empty scan results (not error)
- [x] Connector returns empty data (no repos, no issues) handled gracefully
- [ ] Large org with many repos/issues — no pagination or streaming for connector data
- [ ] Connector auth expired mid-scan — partial results with error not handled

## Security

- [x] Both routes require authentication (401 for unauthenticated)
- [x] Both routes require operator role (403 for non-admin) — `require_permission("determination.scan")`, operator baseline (ADR 017), resolves `get_current_tenant_user`
- [ ] No rate limiting on expensive scan/draft endpoints
- [ ] No input size validation on pipeline draft payload

## Known Gaps

- **No connector-specific error mapping** — connector failures (rate limits, auth expiry) during the scan surface as 500 instead of typed 502/429 responses. `run_scan` already degrades gracefully into per-resource error samples; the remaining gap is surfacing those as structured HTTP errors.
- **Large org scans are unbounded** — no pagination or streaming for connector data (sequential `_SAMPLE_LIMIT=25` queries per connector).
- **No rate limiting** on the expensive scan/draft endpoints.
- **No input size validation** on the pipeline draft payload (route takes no request body today).

## QA History

### 2026-08-13 — improve-architecture (product-map walk, index 177)

**RESOLVED 3 known gaps + closed a stale product-map claim + fixed the route authz gap:**

1. **Operator-role enforcement added** — both routes previously accepted any authenticated tenant member (`Depends(get_current_user)`); the product map claimed "requires operator role" but the code did not enforce it, and the draft route was even listed in the ADR 017 introspection exempt allowlist as "any-authenticated, no system mutation". Both handlers now require `require_permission("determination.scan")` (new `determination.scan: operator` key in `PERMISSIONS`). The scan reads all of the org's connected tool data (repos, PRs, issues) so it is operator-scoped like `schema.infer`. Removed the now-obsolete introspection exempt entry for `POST /api/v1/determination/draft`.
2. **Endpoint-level tests added** — `backend/tests/unit/api/test_determination_endpoint.py` (15 tests): happy path + empty-scan for both routes, unauthenticated 4xx, viewer 403, and the full error matrix (`ProgrammingError` → 501, `SQLAlchemyError` → 503, `ConnectorDecryptError` → 502, generic `Exception` → 500) on both endpoints.
3. **BDD coverage added** — `backend/tests/bdd/features/determination/determination.feature` (4 scenarios) with co-located step definitions: operator scan → 200 with summary + samples, operator draft → 200 with nodes/edges, viewer → 403, empty scan → 200 with empty sample list.
4. **Stale Known Gap removed** — the "Dead code `_load_and_scan()` helper" gap: the function no longer exists in `determination.py` (both routes call `run_scan` directly). Marked resolved.

Verification: 15/15 `test_determination_endpoint.py`, 78/78 determination + auth permission tests, full `tests/unit/api/` (route introspection EXEMPT change verified), ruff check + format clean, mypy --strict clean. Status: partial (connector error mapping, large-org pagination, rate limiting, payload size validation remain).
