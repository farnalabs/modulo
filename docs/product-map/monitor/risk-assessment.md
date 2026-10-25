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
      query params for deep-linking
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
