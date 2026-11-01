---
id: feat-risk-assessment
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/evidence.py
  - frontend/src/views/RiskAssessmentView.vue
  - frontend/src/components/risk-assessment/ActionBadge.vue
  - frontend/src/components/risk-assessment/RiskAssessmentFacets.vue
  - frontend/src/components/risk-assessment/RiskAssessmentSummaryCard.vue
  - frontend/src/components/risk-assessment/RiskAssessmentTable.vue
  - frontend/src/components/risk-assessment/SortIcon.vue
  - frontend/src/stores/riskAssessment.ts
  - frontend/src/lib/riskAssessment.ts
unit-tests:
  - backend/tests/unit/api/test_risk_assessment_routes.py
  - backend/tests/integration/test_risk_assessment_projection_pg.py
  - frontend/src/__tests__/riskAssessmentStore.spec.ts
  - frontend/src/__tests__/RiskAssessmentComponents.spec.ts
  - frontend/src/__tests__/RiskAssessmentView.spec.ts
depends-on:
  - feat-evidence
  - feat-core-evidence-store
status: covered
---

# Risk Assessment Dashboard

The read-only `/risk-assessment` dashboard (FAR-941 sub-scope B) over the core
evidence fact store provides a facet-driven risk analysis surface. The page
renders under the MONITOR sidebar group and exposes key prefix, pipeline, time
window, and action filters that drive server-side filtering through the risk
assessment store. Results are displayed as a summary card with per-action counts
and a sortable evidence table with timestamp, key, action badge, subject, and
formatted value columns. All strings are i18n-backed and status regions use
`role=status`/`aria-live`.

## Behaviours

- [x] The `/risk-assessment` route renders under the MONITOR sidebar group;
      `RiskAssessmentView.vue` fetches risk assessment data via the evidence
      store and renders facets, summary cards, and a sortable table with
      i18n-backed strings and `role=status`/`aria-live` status regions
- [x] Facet filters (key prefix, pipeline, time window, action) drive
      server-side filtering through the risk assessment store and update URL
      query params for deep-linking. Deep-link hydration accepts ONLY values
      the facet vocabulary can express and the wire contract parses: the time
      window must be one of the offered `RISK_WINDOW_CHOICES`, the action must
      be in the gate-action list, and `pipeline_id` must be a canonical UUID
      (`key_prefix` stays free text, which the API treats as a LIKE prefix). An
      out-of-contract value is a malformed link and degrades to the default
      rather than being sent, because the backend answers it 422 and the error
      alert's Retry re-applies the same filters, turning a stale or
      hand-tampered link into an error the retry can never clear. The select
      renders the same `RISK_WINDOW_CHOICES` the hydration accepts, so the
      facet and the deep-link contract cannot drift
      (`frontend/src/lib/riskAssessment.ts`,
      `frontend/src/views/RiskAssessmentView.vue`,
      `frontend/src/components/risk-assessment/RiskAssessmentFacets.vue`,
      `frontend/src/__tests__/RiskAssessmentView.spec.ts`)
- [x] The pipeline facet loads EVERY selectable pipeline, not just the first
      page: the list endpoint ignores ``limit`` and caps ``page_size`` at 100,
      so the view walks the offset pages (``page_size=100``, bounded at 20
      pages) and accumulates the results. A failure on any page after the first
      surfaces the facet error instead of publishing the prefix collected so far
      (a partial dropdown silently hides pipelines from the filter); running
      past the page budget labels the list as truncated rather than presenting
      it as complete
      (`frontend/src/views/RiskAssessmentView.vue`,
      `frontend/src/__tests__/RiskAssessmentView.spec.ts`)
- [x] Summary card displays total rows and per-action counts (block, warn,
      continue, escalate) with facet degradation indicator and time window note
- [x] Sortable table renders evidence rows with timestamp, key, action badge,
      subject, and formatted value columns; empty state handled. The action
      badge renders the gate action resolved server-side from
      `policy_gate_decisions` (never a key-prefix guess), and sortable headers
      are buttons carrying `aria-sort` so keyboard users can sort

## Known Gaps

- Risk assessment write paths (policy gate decisions, risk rules CRUD) are
  tracked under FAR-941 follow-up; this entry covers the read-only dashboard
  surface only

## QA History

- 2026-10-31: **Improve Architecture product-map walk** - fixed MAJOR
  (behaviour-adversarial / Lens 9 - *unreachable-in-tested-states retry*): a
  deep link carrying an out-of-contract value (`?days=400`, `?days=-5`,
  `?action=nonsense`, `?pipeline_id=not-a-uuid`) was hydrated verbatim and sent
  to the API, which answers 422 (`days` is bounded 1..365, `action` is
  vocabulary-checked, `pipeline_id` is a `uuid.UUID` query parameter), and the
  error alert's Retry re-applies the same filters, so the error could never
  clear: a permanently stuck error state reachable from a stale bookmark or a
  hand-tampered URL. Hydration now validates every param against the same
  contract (facet-vocabulary windows, gate-action list, canonical-UUID
  pipeline id; `key_prefix` remains free text) and drops out-of-contract values
  to the defaults, and the offered time windows are the one shared
  `RISK_WINDOW_CHOICES` list the select renders and the hydration accepts.
  Regression tests pin every malformed class degrading to the default fetch and
  every offered window round-tripping.

- 2026-10-26: **Improve Architecture product-map walk** - fixed MAJOR
  (behaviour-adversarial / Lens 9 List B - *listed but vacuously covered*): the
  pipeline facet requested `GET /api/v1/pipelines` with `{ limit: 100, page: 1 }`,
  but that endpoint has no `limit` parameter and defaults `page_size` to 20, so
  the filter dropdown silently offered only the first 20 pipelines on any
  instance with more. The existing spec pinned the wrong request shape
  (`limit: 100`), so the bug was asserted as correct. The view now walks the
  offset pages (`page_size=100`, bounded at 20 pages), refuses to publish a
  partial list when a later page fails, and explicitly labels exhaustion of the
  page budget. Regression tests cover the page walk, the mid-walk failure (no
  partial options) and the truncation label.
