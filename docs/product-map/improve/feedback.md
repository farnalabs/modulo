---
id: feat-feedback
prd: 8.20
adr: []
code:
  - backend/src/modulo/api/routes/feedback.py
  - backend/src/modulo/core/feedback_manager
unit-tests:
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
  - backend/tests/integration/feedback_manager/test_feedback_flow.py
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
depends-on:
  - feat-evals
  - feat-runs
status: covered
---

# Feedback Inbox

Human feedback on pipeline output — the Feedback System (§8.20). Feedback records are
created per run, transition through a validated state machine
(`pending → routing → correcting → resolved`), feed a review inbox and an eval
**proposals queue**, and drive correction runs / eval-gap detection. Surfaces:
`/feedback/inbox` and the `/api/v1/feedback*` API; orchestration lives in
`core/feedback_manager/*` (state-machine guards, org RLS, correction dispatch).

## Behaviours

- [x] `POST /runs/{run_id}/feedback` creates a feedback record (type `human`,
      status `pending`), 201 when the run exists, 404 when it does not, and it
      emits a `feedback_created` audit event without letting audit failure block
      creation (`test_feedback_endpoint.py`)
- [x] `GET /feedback` returns a paginated (page/page_size) list filterable by status;
      `GET /feedback/{record_id}` returns a single record (404 when missing)
- [x] Status updates enforce the transition machine: `pending → routing → correcting →
      resolved`, invalid transitions rejected 4xx, dismissed accepted, and a change
      emits a `feedback_status_changed` audit event (audit failure does not block the
      update)
- [x] `GET /feedback/inbox` returns the paginated review queue filterable by type and
      status, with date-range filtering; `GET /feedback/inbox/{record_id}` and
      `POST /feedback/inbox/{record_id}/review` expose and advance the review workflow
- [x] `POST /feedback/{record_id}/detect-gap` runs eval-gap detection over the feedback
      record, producing an eval proposal (`DetectEvalGap`), and round-trips ORM eval
      definitions through the endpoint
- [x] Proposals: `GET /feedback/proposals` lists the eval proposals queue and
      `POST /feedback/proposals/{record_id}/publish` promotes a proposal to a live eval
      definition (PRD §8.20 "Eval suite growth #3")
- [x] The manager is RLS-gated per org, validators reject malformed inputs, and the
      state machine rejects out-of-order transitions (`feedback_system.feature`,
      `test_feedback_flow.py`)

## Known Gaps

- **No standalone BDD step file for the inbox/proposals endpoints** — the
  `feedback_system.feature` BDD covers the record state machine; inbox, review,
  detect-gap and proposals are covered only by `test_feedback_endpoint.py` +
  `core/feedback_manager/test_feedback_manager.py`.
- **Detection is model-assisted** — eval-gap detection depends on a configured model
  backend; there is no deterministic fallback classifier for gap detection.

## QA History

- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-feedback`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/feedback.py`,
  `core/feedback_manager/*`, `test_feedback_endpoint.py` and the feedback BDD/integration
  suites. Status: covered.