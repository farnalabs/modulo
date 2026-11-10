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
  - backend/src/modulo/determination/comparison.py
  - backend/src/modulo/determination/rerun.py
  - frontend/src/views/DeterminationView.vue
  - frontend/src/components/determination/DeterminationScanPanel.vue
  - frontend/src/components/determination/DeterminationProposalPanel.vue
  - frontend/src/components/determination/DeterminationReviewPanel.vue
  - frontend/src/components/determination/DeterminationComparisonPanel.vue
  - frontend/src/components/determination/DeterminationComparisonProvenance.vue
  - frontend/src/components/determination/DeterminationRerunPanel.vue
  - frontend/src/components/determination/DeterminationRerunAttempt.vue
  - frontend/src/components/determination/DeterminationRerunSourceSummary.vue
  - frontend/src/components/determination/DeterminationEvidenceRefs.vue
  - frontend/src/components/determination/DeterminationEvidencePanel.vue
  - frontend/src/components/determination/DeterminationProvenanceTrail.vue
  - frontend/src/components/determination/evidencePresentation.ts
  - frontend/src/components/determination/confidencePresentation.ts
  - frontend/src/components/determination/comparisonPresentation.ts
  - frontend/src/components/determination/rerunPresentation.ts
  - frontend/src/composables/useDetermination.ts
  - frontend/src/router/index.ts
  - frontend/src/manifest.yaml
unit-tests:
  - backend/tests/unit/api/test_determination_endpoint.py
  - backend/tests/unit/api/test_determination_error_handling.py
  - backend/tests/unit/api/test_determination_review.py
  - backend/tests/unit/api/test_determination_comparison.py
  - backend/tests/unit/api/test_determination_rerun.py
  - backend/tests/unit/determination/test_draft.py
  - backend/tests/unit/determination/test_inference.py
  - backend/tests/unit/determination/test_proposal_contract.py
  - backend/tests/unit/determination/test_review.py
  - backend/tests/unit/determination/test_scanner.py
  - backend/tests/unit/determination/test_comparison.py
  - backend/tests/unit/determination/test_rerun.py
  - frontend/src/__tests__/DeterminationView.spec.ts
  - frontend/src/__tests__/determination/DeterminationEvidence.spec.ts
  - frontend/src/__tests__/determination/DeterminationReviewPanel.spec.ts
  - frontend/src/__tests__/determination/DeterminationComparisonPanel.spec.ts
  - frontend/src/__tests__/determination/DeterminationRerunPanel.spec.ts
  - frontend/src/__tests__/determination/DeterminationPanels.spec.ts
  - frontend/src/__tests__/determination/useDetermination.spec.ts
bdd:
  - backend/tests/bdd/features/determination/determination.feature
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
- [x] Migration comparison (FAR-2473): the `/determination` surface holds a generated draft as the current baseline in memory (its attempt id and proposal id shown, with clear and replace controls), compares it with the latest generated draft as the proposed target through `POST /api/v1/determination/compare`, and renders added, removed and changed nodes, edges and properties - each changed property naming the property plus both values - with each side's provenance kinds, evidence references, confidence and uncertainty. Current and proposed content carry distinct side badges; approval and automation changes are rendered only from the categories the response contains; structurally identical proposals show an explicit empty state; a failed comparison surfaces the 422 verbatim in an assertive live region and clears the previous result; and the panel states that comparing activates, publishes and mutates nothing (`frontend/src/components/determination/DeterminationComparisonPanel.vue`, `frontend/src/components/determination/DeterminationComparisonProvenance.vue`, `frontend/src/components/determination/comparisonPresentation.ts`, `frontend/src/composables/useDetermination.ts`, `backend/src/modulo/determination/comparison.py`).
- [x] Rerun determination (FAR-2474): `POST /api/v1/determination/rerun` sends the proposal on screen as the prior attempt plus the review panel's current corrections (corrected element edits and removed element ids, carried forward by prior element id) and renders the `RerunResult` - both attempts' id, proposal id, status, confidence, uncertainty, unavailable evidence and provenance; the per-inference classification as six distinct groups (retained, new, not_reproduced, removed, confidence_changed, evidence_changed) with counts and both sides' confidence and evidence references. Not reproduced is rendered quietly with an explicit statement that the unchanged inputs simply did not reproduce the inference - it is never styled or worded as removed, false or wrong - while removed is the only destructive styling and explains that the sampled inputs changed. Input changes render new sources, missing sources, newly available, newly unavailable and sample-count changes, with an explicit inputs-unchanged state when the set did not change. Carried-forward corrections render matched ids and every unmatched correction id with its reason. An incomplete result (completed_with_gaps, missing sources or a source that became unavailable) is a loud warning banner listing the returned reasons, never presented as authoritative; errors use aria-live=assertive and status uses aria-live=polite. The panel states that a rerun creates a new attempt and changes nothing - no activation, no publish, no mutation of the prior attempt (`frontend/src/components/determination/DeterminationRerunPanel.vue`, `frontend/src/components/determination/DeterminationRerunAttempt.vue`, `frontend/src/components/determination/DeterminationRerunSourceSummary.vue`, `frontend/src/components/determination/rerunPresentation.ts`, `frontend/src/composables/useDetermination.ts`, `backend/src/modulo/determination/rerun.py`).

## Deferrals

- Proposal activation (creating or enabling a pipeline from an approved proposal) is deliberately out of scope: the contract and the review response pin `activation_status` to `not_activated`.

## QA History

- 2026-11-10: **Improve Architecture product-map walk** - wired `determination.feature` into the executing BDD suite for proposal review decisions (approval with acknowledged assumptions, reject requiring a non-empty reason, and approval blocked by unacknowledged low-confidence assumptions), proposal comparison, and determination rerun. Replaced `bdd: []` with the executing feature file citation, updated frontmatter `code:` and `unit-tests:` citations with the comparison and rerun modules, and aligned tracked behaviors with the shipped comparison (FAR-2473) and rerun (FAR-2474) surfaces.
