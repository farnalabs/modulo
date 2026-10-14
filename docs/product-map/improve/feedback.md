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
  - backend/tests/bdd/features/eval/feedback_inbox.feature
depends-on:
  - feat-evals
  - feat-runs
status: covered
---

# Feedback Inbox

Human feedback on pipeline output — the Feedback System (§8.20). Feedback records are
created per run, transition through a validated state machine
(`pending → routing → correcting → resolved`, with `escalated` and `dismissed`
also reachable), feed a review inbox and an eval
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
- [x] Status updates enforce the transition machine (`VALID_STATUS_TRANSITIONS`:
      `pending`/`routing`/`correcting` → `resolved` or `dismissed`, plus
      `routing`/`correcting` → `escalated`), an unknown status value is 422 and an
      out-of-order transition is rejected by the manager guard
      (`InvalidTransitionError`), and a change records a `feedback.status_changed`
      audit event (the PATCH route also carries the coarse
      `feedback_status_updated` audited dependency); audit failure does not block
      the update
- [x] `GET /feedback/inbox` returns the paginated review queue filterable by type and
      status, with date-range filtering; `GET /feedback/inbox/{record_id}` and
      `POST /feedback/inbox/{record_id}/review` expose and advance the review workflow
- [x] `POST /feedback/{record_id}/detect-gap` runs eval-gap detection over the feedback
      record, producing an eval proposal (`FeedbackManager.detect_eval_gap`), and
      round-trips ORM eval definitions through the endpoint
- [x] Proposals: `GET /feedback/proposals` lists the eval proposals queue and
      `POST /feedback/proposals/{record_id}/publish` promotes a proposal to a live eval
      definition (PRD §8.20 "Eval suite growth #3")
- [x] Feedback records are org-scoped (every route sets the RLS context and the
      manager's queries carry an explicit `organisation_id` predicate), validators
      reject malformed inputs, and the state machine rejects out-of-order
      transitions (`feedback_system.feature`, `test_feedback_flow.py`)

## Known Gaps

- **Gap detection is deterministic; correction runs are model-assisted** — eval-gap
  detection replays the pipeline's existing eval suite (a record whose output no
  existing eval catches is flagged `eval_gap`, and a record whose pipeline has an
  empty suite is flagged unconditionally), so it needs no model backend; it is the
  correction-run path that resolves a configured model backend.

## QA History

- 2026-10-10: **product-map review pass** — reconciled the tracker with the
  shipped code: corrected the status-machine description (added `escalated`),
  fixed the audit-event name (`feedback.status_changed`, not the nonexistent
  `feedback_status_changed`), replaced the phantom `DetectEvalGap` identifier with
  `FeedbackManager.detect_eval_gap`, corrected the "model-assisted detection" Known
  Gap (detection replays the existing eval suite deterministically — the model
  backend belongs to the correction path), and softened the manager-RLS claim to
  the route+query org-scoping that actually holds.
- 2026-09-25: **Improve Architecture product-map walk** — reconciled the
  manifest `feat-feedback` registry entry with this tracker (both now
  `status: covered`): the unchecked "gap detection partially wired" item from #972
  is closed — detection, the proposals queue and publish all ship
  (`POST /api/v1/feedback/{record_id}/detect-gap`, `GET /api/v1/feedback/proposals`,
  `POST /api/v1/feedback/proposals/{record_id}/publish`; `test_feedback_endpoint.py`).
  The model-assisted detection limitation stays as a Known Gap.
- 2026-09-18: **product-map review pass** — closed the "No standalone
  BDD step file for the inbox/proposals endpoints" gap. Registered
  `eval/feedback_inbox.feature` into the executing BDD suite from the new
  `steps/test_feedback_inbox.py`, driving the real `/api/v1/feedback/inbox`
  (pipeline-name enrichment + type/status filter passthrough), inbox-item detail,
  `/inbox/{id}/review` (`mark_reviewed` → resolved, `dismiss` → dismissed,
  `create_correction_run` → spawned correction run, invalid action → 422, missing
  record → 404), `/{id}/detect-gap` (eval_gap=true via the real route + manager seam),
  `/feedback/proposals` (eval-gap queue), and `/proposals/{id}/publish`
  (201 pipeline/node-scoped EvalDefinition + resolved transition, 422 non-gap,
  409 non-pending, 404 missing). `_ORPHANED_BDD_FEATURES` stays empty.

- 2026-09-12: **product-map review pass** — registered the shared
  `JsonViewer` surface (`components/shared/JsonViewer.vue` static testids
  `json-viewer` / `json-viewer-{copy,expand-all,collapse-all,string-expand,string-collapse}`)
  in the manifest `elements:` inventory for `/feedback/inbox`: the feedback record's
  rejected output and correction proposal render inline with
  `<JsonViewer :show-toolbar="true">` (`FeedbackInboxView.vue`), so the viewer
  shipped in the DOM while staying invisible to Assistant's docs indexer /
  `/api/v1/manifest`. The component is now part of the route's reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/feedback/inbox`: the whole-page view(s) `FeedbackInboxView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-28: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-feedback`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/feedback.py`,
  `core/feedback_manager/*`, `test_feedback_endpoint.py` and the feedback BDD/integration
  suites. Status: covered.
