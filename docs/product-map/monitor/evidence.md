---
id: feat-evidence
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/evidence.py
  - backend/src/modulo/core/evidence_redaction.py
  - backend/src/modulo/db/models/evidence_redaction_rule.py
  - backend/src/modulo/db/migrations/versions/0320_evidence_redaction_rules.py
  - frontend/src/views/EvidenceView.vue
  - frontend/src/views/AdminEvidenceRedactionView.vue
unit-tests:
  - backend/tests/unit/api/test_evidence_routes.py
  - backend/tests/unit/core/evidence/test_evidence_redaction.py
  - backend/tests/integration/db/test_evidence_redaction_rules.py
  - frontend/src/__tests__/AdminEvidenceRedactionView.spec.ts
depends-on:
  - feat-core-evidence-store
status: covered
---

# Evidence Dashboard & Redaction Boundary

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
