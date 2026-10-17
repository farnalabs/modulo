---
id: feat-evidence
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/evidence.py
  - backend/src/modulo/core/evidence_redaction.py
  - backend/src/modulo/db/models/evidence_redaction_rule.py
  - backend/src/modulo/db/migrations/versions/0319_evidence_redaction_rules.py
  - frontend/src/views/EvidenceView.vue
unit-tests:
  - backend/tests/unit/api/test_evidence_routes.py
  - backend/tests/unit/core/evidence/test_evidence_redaction.py
  - backend/tests/integration/db/test_evidence_redaction_rules.py
depends-on:
  - feat-core-evidence-store
status: partial
---

# Evidence Dashboard & Redaction Boundary

The read-only `/evidence` dashboard (FAR-944 chunk 9b) over the core evidence
fact store, with a SERVER-SIDE field-level redaction boundary: a caller's role
resolves a rule from `evidence_redaction_rules` (most-specific-wins by
`key_pattern`, else the `*` catch-all for that role, else the built-in viewer
default) and the covered evidence-value keys are replaced with the
`[REDACTED]` sentinel before the payload is serialised. Redaction is name-based
(a class covers a fixed set of JSON key NAMES), not a default-deny allowlist.
The `/evidence` route renders under the MONITOR sidebar group.

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
      `(key_pattern, role, redacted_fields)` — gated by
      `evidence.redaction.manage` (admin) via `require_system_or_org_admin`,
      with unknown redaction classes rejected 422 (a rule can never be a silent
      no-op); writes are audited (`evidence_redaction_rule_upserted`)
- [x] Migration `0319_evidence_redaction_rules` creates `evidence_redaction_rules`
      (OrgScoped + soft-delete, `UNIQUE (organisation_id, key_pattern, role)`,
      role CHECK in admin/operator/viewer), applies the four-step RLS pattern,
      and seeds the mandatory catch-all row `key_pattern='*'` / `role='viewer'` /
      `redacted_fields='{sensitive_output, internal_metadata}'` for every
      existing organisation
- [x] The `/evidence` route renders under the MONITOR sidebar group;
      `EvidenceView.vue` fetches the already-redacted payload and renders it
      as-is with NO client-side redaction or conditional field hiding, with
      i18n-backed strings and `role=status`/`aria-live` status regions

## Deferrals

- [ ] The redaction-rules `GET`/`PUT` surface ships as an admin API only
      (chunk 9b §2.5); there is no in-product admin rule-editor UI yet — the
      `/evidence` view is a read-only dashboard, and the catch-all seed plus the
      service's built-in viewer default keep viewers redacted without any rule
      authored
