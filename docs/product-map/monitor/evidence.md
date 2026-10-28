---
id: feat-evidence
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/evidence.py
  - backend/src/modulo/api/routes/compliance.py
  - backend/src/modulo/core/evidence_redaction.py
  - backend/src/modulo/compliance/evidence_index.py
  - backend/src/modulo/compliance/pack.py
  - backend/src/modulo/db/models/evidence_redaction_rule.py
  - backend/src/modulo/db/migrations/versions/0320_evidence_redaction_rules.py
  - frontend/src/views/EvidenceView.vue
  - frontend/src/views/ComplianceEvidenceIndexView.vue
  - frontend/src/views/AdminEvidenceRedactionView.vue
unit-tests:
  - backend/tests/unit/api/test_evidence_routes.py
  - backend/tests/unit/api/test_compliance_endpoint.py
  - backend/tests/unit/core/evidence/test_evidence_redaction.py
  - backend/tests/unit/compliance/test_evidence_index.py
  - backend/tests/integration/db/test_evidence_redaction_rules.py
  - backend/tests/integration/test_compliance_evidence_index.py
  - frontend/src/__tests__/EvidenceView.spec.ts
  - frontend/src/__tests__/ComplianceEvidenceIndexView.spec.ts
  - frontend/src/__tests__/AdminEvidenceRedactionView.spec.ts
depends-on:
  - feat-core-evidence-store
status: covered
---

# Evidence Dashboard, Compliance Index & Redaction Boundary

The read-only `/evidence` dashboard (FAR-944 chunk 9b) over the core evidence
fact store, with a SERVER-SIDE field-level redaction boundary: a caller's role
resolves a rule from `evidence_redaction_rules` (most-specific-wins by
`key_pattern`, else the `*` catch-all for that role, else the built-in
fail-closed default) and the covered evidence-value keys are replaced with the
`[REDACTED]` sentinel before the payload is serialised. Redaction is name-based
(a class covers a fixed set of JSON key NAMES), not a default-deny allowlist.
The `/evidence` route renders under the MONITOR sidebar group, and the
`/admin/evidence-redaction` editor under the ADMIN sidebar group lets an admin
maintain the rules the boundary consults.

The same feature owns the per-instance **compliance evidence index** (FAR-2036):
`/admin/compliance/evidence-index` renders pack metadata, framework coverage and
one entry per artefact type the instance produces, keyed to the mapping rows the
artefact satisfies, with the deprecated `/admin/evidence` URL kept as a
compatibility alias to the same view.

## Behaviours

- [x] `GET /api/v1/evidence` lists a run's evidence rows (required `run_id` UUID;
      optional `node_id` and `key` filters) scoped to the caller's organisation
      through RLS, with the field-level redaction boundary applied server-side
      before serialisation; permitted fields (`key`,
      `subject_type`/`subject_id`, `observed_at`, timestamps, pass/fail
      outcomes) survive unredacted
- [x] `GET /api/v1/evidence/{evidence_id}` returns a single org-scoped evidence
      row with the same server-side redaction applied and 404s an id absent for
      the org
- [x] Redaction rules are readable and editable by org admins only:
      `GET /api/v1/evidence/redaction-rules` lists the org's rules and
      `PUT /api/v1/evidence/redaction-rules` upserts one by
      `(key_pattern, role, redacted_fields)`, gated by
      `evidence.redaction.manage` (admin) via `require_system_or_org_admin`,
      with unknown redaction classes rejected 422 (a rule can never be a silent
      no-op); writes are audited (`evidence_redaction_rule_upserted`)
- [x] Migration `0320_evidence_redaction_rules` creates `evidence_redaction_rules`
      (OrgScoped + soft-delete, `UNIQUE (organisation_id, key_pattern, role)`,
      role CHECK in admin/operator/viewer), applies the four-step RLS pattern,
      and seeds the mandatory catch-all row `key_pattern='*'` / `role='viewer'` /
      `redacted_fields='{sensitive_output, internal_metadata}'` for every
      existing organisation
- [x] The `/evidence` route renders under the MONITOR sidebar group;
      `EvidenceView.vue` fetches the already-redacted payload and renders it
      as-is with NO client-side redaction or conditional field hiding, with
      i18n-backed strings and `role=status`/`aria-live` status regions
- [x] The `/admin/evidence-redaction` route (ADMIN sidebar group, admin role)
      renders `AdminEvidenceRedactionView.vue`, which lists the org's rules and
      creates or edits them through the same
      `PUT /api/v1/evidence/redaction-rules` upsert keyed on
      `(key_pattern, role)`: a create needs an unused pair and at least one
      redacted class, both rejected inline before any request; an edit keeps
      `key_pattern` and `role` fixed and changes only `redacted_fields`; and
      there is no delete control (the API has no delete endpoint either). The
      class checkboxes are limited to the backend `REDACTION_CLASS_FIELDS`
      vocabulary, the seeded `*` / `viewer` catch-all is marked mandatory (with
      a warning when it is absent), and the page configures the SERVER boundary
      only - it never redacts or hides evidence values in the client
- [x] `GET /api/v1/compliance/evidence-index` returns the organisation's
      per-instance evidence index, gated by `compliance.evidence_index.read`,
      scoped to the caller's organisation from the authenticated principal
      (never a query parameter) through RLS, and optionally scoped to an audit
      period with the half-open `from`/`to` parameters (a start after the end is
      rejected 422; no period means the organisation's full history, recorded as
      null `period_from`/`period_to`). On Postgres the transaction carries a
      30s transaction-local `statement_timeout` and a timeout is answered 503
      with an audit-period remedy, not a pinned connection. Each artefact type
      the instance produces is returned with its status/provenance/record
      count/location; an absent type is returned as `absent` and named in
      `missing_artifact_types`, customer-owned record types are deliberately
      omitted, and `pack_version`/`pack_content_hash` pin which pack produced
      the index
- [x] The canonical `/admin/compliance/evidence-index` route renders
      `ComplianceEvidenceIndexView.vue`, with the deprecated `/admin/evidence`
      route kept as a compatibility alias to that same view. The page shows pack
      metadata, framework coverage, present/absent/customer-supplied artefact
      status, identifiers/locations/provenance/mapping row IDs, missing artefact
      warnings and the human-only lower-bound warning; a pipeline-snapshot
      identifier renders as a concise label plus a link to that exact version
      and the raw identifier behind a details disclosure with a copy action,
      while every other identifier stays verbatim

## Redaction rule editor

The editor is the human surface over the `GET`/`PUT`
`/api/v1/evidence/redaction-rules` endpoints. Because the `PUT` is an upsert,
the page is careful about identity:

- **Create** requires a `(key_pattern, role)` pair that is not already listed
  and at least one redacted class. The page rejects both problems inline, so a
  new rule cannot silently overwrite an existing pair's classes, and a
  zero-class rule cannot silently turn redaction off for every key it matches.
- **Edit** changes only `redacted_fields`. The `key_pattern` and `role` inputs
  are disabled while editing, and the page has no delete: changing a rule's
  identity means creating a new rule and keeping the old one.
- The seeded catch-all (`key_pattern='*'`, `role='viewer'`) is badged as
  mandatory, and the page warns when that row is missing. The badge does not
  make it global: rules match per role, so the catch-all is the viewer
  fallback; other untrusted roles with no rule of their own fall back to the
  built-in default (both classes redacted), while `admin` and `operator` are
  trusted and see unredacted evidence when no rule targets them.
- The class vocabulary is the backend's `REDACTION_CLASS_FIELDS`
  (`sensitive_output`, `internal_metadata`). An unrecognised stored class stays
  visible so it can be removed, and an unrecognised stored role is shown
  read-only with Save disabled.
- The page never redacts or hides evidence values itself; it only configures
  the server-side boundary.

## Known Gaps

- There is no delete endpoint or delete control for a redaction rule, so a
  retired rule stays in the list. The editor can change its classes, and the
  upsert restores a soft-deleted rule in place, but neither surface deletes
  one.
- Risk/Assessment (sub-scope B, FAR-941) is tracked by a separate ticket and is
  not part of this entry; the decision-record NULL-duplicate grouping
  (acceptance criteria 12-14) belongs to the decision-record read path and is
  not implemented here.
- `frontend/src/views/EvidenceIndexView.vue` is a superseded view: the
  `/admin/evidence` URL now renders `ComplianceEvidenceIndexView.vue` and no
  router entry mounts the old component, but it and its
  `frontend/src/__tests__/evidence-index.spec.ts` remain in the tree. It is not
  part of the shipped surface and carries no product-map element inventory.

## QA History

- 2026-10-28: **Improve Architecture product-map walk** (first pass on this
  entry). Fixed MINOR (product-map completeness): the `/evidence` page shipped
  six static testids (`evidence-run-id`, `evidence-node-id`, `evidence-key`,
  `evidence-load`, `evidence-loading`, `evidence-count`) plus the shared
  `ErrorAlert` retry, but the manifest had **no `elements` inventory for the
  route at all** - so the page's whole testable surface was invisible to
  Assistant's `search_documentation` indexer and to `/api/v1/manifest`. The
  blind spot was structural: `OWNED_PAGES` in
  `backend/tests/architecture/test_product_map_consistency.py` (the map that
  drives both the reverse-testid-coverage guard and the phantom-element guard)
  omitted `/evidence`, so `test_mapped_route_elements_cover_owning_view_testids`
  proved nothing about the page even though its docstring claims to cover
  "every whole-page view component". Registered the inventory and added
  `/evidence` to `OWNED_PAGES`, so a future testid on the page must be
  documented or CI fails. Also added the shipped
  `frontend/src/__tests__/EvidenceView.spec.ts` (18 specs covering the load
  form, blank-run-id rejection, no-fetch-on-mount, auto-load from query params,
  redacted-value rendering, loading/empty/error states, retry, and
  circular/undefined value formatting) to the frontmatter - it existed but was
  not listed. Verified the rest of the entry against the code: the list/detail
  reads are org-scoped and redacted server-side, the rule upsert is
  admin-gated and audited, migration 0320 + org creation both seed the
  `*`/viewer catch-all, and the editor's create/edit guards match the
  documented contract. No behaviour change.

- 2026-10-28: **Improve Architecture product-map walk** (second pass on this
  entry, compliance evidence index). Fixed MINOR (product-map completeness +
  stale inventory): the compliance index view shipped two focusable scroll
  regions (`compliance-evidence-index-coverage-scroll`,
  `compliance-evidence-index-artifacts-scroll`) and the shared
  `error-alert-retry` (the load error passes `:on-retry`), but none were in the
  manifest `elements` inventory, and `/admin/compliance/evidence-index` was
  absent from `OWNED_PAGES` in
  `backend/tests/architecture/test_product_map_consistency.py` - so
  `test_mapped_route_elements_cover_owning_view_testids` never checked the
  page's static testids and a future control could ship invisible to
  `/api/v1/manifest` and Assistant's docs indexer. Registered the three
  elements and added the route to `OWNED_PAGES`. Also fixed the deprecated
  `/admin/evidence` inventory: it still listed `EvidenceIndexView.vue` testids
  (`evidence-index-page`, `page-header-right`, `evidence-index-refresh`,
  `evidence-index-loading`) although that URL has rendered
  `ComplianceEvidenceIndexView.vue` since the canonical route shipped - a
  phantom surface the guard could not see because the alias was not in
  `OWNED_PAGES`. Dropped the stale block; the canonical route owns the
  inventory. Verified the tracker against the code: the route is gated by
  `compliance.evidence_index.read`, org comes from the principal through RLS,
  `from`/`to` are half-open with a 422 on an inverted period, the Postgres
  statement timeout is transaction-local, customer-owned record types are
  omitted by design, and the alias resolves to the same view. No behaviour
  change.
