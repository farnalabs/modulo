---
id: feat-core-determination
prd: 8.16
code:
  - backend/src/modulo/api/routes/determination.py
  - backend/src/modulo/determination/draft.py
  - backend/src/modulo/determination/inference.py
  - backend/src/modulo/determination/scanner.py
bdd: []
unit-tests:
  - backend/tests/unit/determination/test_draft.py
  - backend/tests/unit/determination/test_inference.py
  - backend/tests/unit/determination/test_scanner.py
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

## Error Handling

- [x] Both routes catch `ProgrammingError` → 501 with migration hint
- [x] Both routes catch `SQLAlchemyError` → 503
- [x] Both routes catch `IntegrityError` → 409
- [x] Both routes catch `HTTPException` → re-raise
- [x] Both routes catch `Exception` → 500 with `logger.exception`
- [ ] No connector-specific error handling — connector failures (rate limits, auth expiry) propagate as 500

## Edge Cases

- [x] No connectors configured returns empty scan results (not error)
- [x] Connector returns empty data (no repos, no issues) handled gracefully
- [ ] Large org with many repos/issues — no pagination or streaming for connector data
- [ ] Connector auth expired mid-scan — partial results with error not handled

## Security

- [x] Both routes require authentication (401 for unauthenticated)
- [x] Both routes require operator role (403 for non-admin)
- [ ] No rate limiting on expensive scan/draft endpoints
- [ ] No input size validation on pipeline draft payload

## Known Gaps

- **No BDD coverage** — No .feature files for determination endpoints.
- **Dead code** — `_load_and_scan()` helper function in determination.py is defined but never called by either route.
- **Unit tests exist for core logic** — `test_draft.py`, `test_inference.py`, `test_scanner.py` cover the determination module, but no endpoint-level tests exist for the two route handlers.
