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

## Known Gaps

- **No BDD coverage** — No .feature files for determination endpoints.
- **Dead code** — `_load_and_scan()` helper function in determination.py is defined but never called by either route.
- **Unit tests exist for core logic** — `test_draft.py`, `test_inference.py`, `test_scanner.py` cover the determination module, but no endpoint-level tests exist for the two route handlers.
