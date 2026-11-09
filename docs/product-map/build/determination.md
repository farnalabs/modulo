---
id: feat-determination
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/determination.py
  - backend/src/modulo/determination/models.py
  - backend/src/modulo/determination/scanner.py
  - backend/src/modulo/determination/inference.py
  - backend/src/modulo/determination/draft.py
  - backend/src/modulo/determination/review.py
  - frontend/src/views/DeterminationView.vue
  - frontend/src/components/determination/DeterminationScanPanel.vue
  - frontend/src/components/determination/DeterminationProposalPanel.vue
  - frontend/src/components/determination/DeterminationReviewPanel.vue
  - frontend/src/components/determination/DeterminationEvidenceRefs.vue
  - frontend/src/components/determination/DeterminationEvidencePanel.vue
  - frontend/src/components/determination/DeterminationProvenanceTrail.vue
  - frontend/src/components/determination/evidencePresentation.ts
  - frontend/src/components/determination/confidencePresentation.ts
  - frontend/src/composables/useDetermination.ts
  - frontend/src/router/index.ts
  - frontend/src/manifest.yaml
unit-tests:
  - backend/tests/unit/api/test_determination_endpoint.py
  - backend/tests/unit/api/test_determination_error_handling.py
  - backend/tests/unit/api/test_determination_review.py
  - backend/tests/unit/determination/test_draft.py
  - backend/tests/unit/determination/test_inference.py
  - backend/tests/unit/determination/test_proposal_contract.py
  - backend/tests/unit/determination/test_review.py
  - backend/tests/unit/determination/test_scanner.py
  - frontend/src/__tests__/DeterminationView.spec.ts
  - frontend/src/__tests__/determination/DeterminationEvidence.spec.ts
  - frontend/src/__tests__/determination/DeterminationReviewPanel.spec.ts
bdd: []
delivery-tasks: []
depends-on:
  - feat-connectors
  - feat-core-evidence-store
status: covered
---

# Determination (`/determination`)

The read-only SDLC determination surface, reachable under the BUILD sidebar
group behind the `determination.scan` permission (operator floor). It samples
the organisation's connected sources, infers lifecycle findings from what it
observed, turns those findings into an editable pipeline proposal, and records
a reviewer's decision on that proposal. Nothing on this route activates a
proposal: producing or reviewing a proposal never creates a pipeline, enables a
trigger, or writes to a connected tool.

## Behaviors

- [x] The `/determination` route runs `GET /api/v1/determination` and renders every sampled source (connector type, resource, record count, connector id, error) and every inferred finding (category, finding, evidence, confidence, uncertainty) with a polite live loading region and a retryable error alert (`frontend/src/views/DeterminationView.vue`, `frontend/src/components/determination/DeterminationScanPanel.vue`).
- [x] Generate draft calls `POST /api/v1/determination/draft` and renders the returned `alpha.determination-proposal.v1` `ProposalContract`: contract version, attempt status, overall confidence, review state, the uncertainty list, observed facts, derived elements, unresolved assumptions, unavailable evidence, provenance references and the attempt source inventory; every fact, derived element and assumption lists its own evidence references (`frontend/src/components/determination/DeterminationProposalPanel.vue`, `frontend/src/components/determination/DeterminationEvidenceRefs.vue`).
- [x] The review panel records `approve`, `reject` or `needs_review` through `POST /api/v1/determination/review`; a reject is refused client-side while the reason is blank, and a refused decision surfaces the server's 422 ProblemDetail detail verbatim in an assertive live region (`frontend/src/components/determination/DeterminationReviewPanel.vue`, `frontend/src/composables/useDetermination.ts`).
- [x] Approval is blocked while any low or unknown-confidence unresolved assumption is unacknowledged: each blocking assumption renders an acknowledgement checkbox, the approve control stays disabled until all are acknowledged, and the remaining count is announced politely (`frontend/src/components/determination/DeterminationReviewPanel.vue`).
- [x] Derived elements are correctable before submission: statement and confidence are editable in place, an element can be removed and restored, and only actual changes are sent as `corrected_derived_elements` alongside `removed_derived_element_ids`; a blank statement blocks every decision (`frontend/src/components/determination/DeterminationReviewPanel.vue`, `frontend/src/composables/useDetermination.ts`).
- [x] The backend refuses an approve while an unacknowledged low/unknown-confidence assumption remains (422), refuses an acknowledged id or corrected element that is not part of the proposal, revalidates corrections as a full `ProposalContract`, and pins `activation_status` to `not_activated` (`backend/src/modulo/determination/review.py`).
- [x] The decision is recorded as one audit-chain event (`determination.proposal_review_decided`, resource type `determination_proposal`) carrying the decision, reason, acknowledged assumption ids, corrected/removed derived-element ids, `decided_at` and the actor; the route reuses the operator-scope `determination.scan` permission rather than adding a new key (`backend/src/modulo/api/routes/determination.py`).
- [x] A recorded decision shows `review_state`, the deciding actor, `decided_at` and an explicit not-activated statement; generating a new draft clears the previous result because the proposal id changed (`frontend/src/components/determination/DeterminationReviewPanel.vue`, `frontend/src/composables/useDetermination.ts`).
- [x] Each evidence reference is a per-element drill-down: a toggle (with `aria-expanded` / `aria-controls`) reveals the matching sampled source record from the attempt source inventory (connector type, resource, record count, status and error) without leaving the element, cross-links to that source row with a "Locate source record" control that highlights and scrolls to it, and an unmatched `source_id` says so explicitly instead of rendering nothing; each source row in the provenance trail's source inventory shows how many element-level evidence references cite it (`frontend/src/components/determination/DeterminationEvidenceRefs.vue`, `frontend/src/components/determination/DeterminationProposalPanel.vue`, `frontend/src/components/determination/DeterminationProvenanceTrail.vue`).
- [x] Per-element evidence drill-down (FAR-2472): selecting an observed fact, derived element or unresolved assumption opens a modal panel naming the element kind, its statement, its confidence and (for an assumption) its own uncertainty, and listing every evidence reference with its reference id, source id, locator and provenance; an element with no references states that explicitly instead of showing an empty panel. The panel is a native modal dialog (focus-contained, accessible name, Escape and a Close button both dismiss it) and is keyed by element id, so removing or correcting one element never renumbers another's references (`frontend/src/components/determination/DeterminationEvidencePanel.vue`, `frontend/src/components/determination/DeterminationEvidenceRefs.vue`, `frontend/src/components/determination/evidencePresentation.ts`).
- [x] The proposal carries a provenance trail for the proposal as a whole (FAR-2472): the determination attempt id and status, the contract version, the overall confidence and uncertainty, the proposal-level provenance references, the full source inventory (source id, connector type, resource, sample count, available/unavailable plus the connector error) and, once a review decision exists, the decision record (decision, deciding actor, decided_at) - with an explicit no-decision state before one is recorded (`frontend/src/components/determination/DeterminationProvenanceTrail.vue`).
- [x] Observed and derived content are never collapsed (FAR-2472): every proposal element renders its kind badge (Observed / Derived / Unresolved assumption) and every evidence reference renders its own provenance badge, with observed, derived/inferred and unavailable styled distinctly; unavailable evidence stays a first-class section with its requested resource and reason. Locators, reasons and connector errors are rendered as text (never v-html) and a value that looks like a credential is replaced with an explicit withheld notice rather than displayed (`frontend/src/components/determination/evidencePresentation.ts`).

## Deferrals

- Proposal activation (creating or enabling a pipeline from an approved proposal) is deliberately out of scope: the contract and the review response pin `activation_status` to `not_activated`.
