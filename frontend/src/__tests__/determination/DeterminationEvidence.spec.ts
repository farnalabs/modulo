import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import DeterminationEvidencePanel from '../../components/determination/DeterminationEvidencePanel.vue'
import DeterminationProposalPanel from '../../components/determination/DeterminationProposalPanel.vue'
import {
  displayEvidenceText,
  elementKindBadgeClass,
  looksLikeSecret,
  provenanceBadgeClass,
  unavailableEvidenceKey,
  type EvidenceElementView,
} from '../../components/determination/evidencePresentation'
import type {
  DeterminationProposal,
  DeterminationReviewResponse,
} from '../../composables/useDetermination'

const PROPOSAL_ID = '11111111-1111-4111-8111-111111111111'
const ATTEMPT_ID = '66666666-6666-4666-8666-666666666666'
const OBSERVED_ID = '22222222-2222-4222-8222-222222222222'
const DERIVED_A_ID = '33333333-3333-4333-8333-333333333333'
const DERIVED_B_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const ASSUMPTION_ID = '44444444-4444-4444-8444-444444444444'

const SECRET_LOCATOR = 'token=ghp_abcdefghijklmnopqrstuvwxyz012345'

function ref(
  referenceId: string,
  sourceId: string,
  locator: string,
  provenance: 'observed' | 'derived' | 'inferred' | 'unavailable' = 'observed',
) {
  return { reference_id: referenceId, source_id: sourceId, locator, provenance }
}

function proposal(): DeterminationProposal {
  return {
    contract_version: 'alpha.determination-proposal.v1',
    proposal_id: PROPOSAL_ID,
    attempt: {
      id: ATTEMPT_ID,
      status: 'completed_with_gaps',
      source_inventory: [
        {
          source_id: 'c-1:repos',
          connector_type: 'github',
          resource: 'repos',
          sample_count: 3,
          status: 'available',
          error: null,
        },
        {
          source_id: 'c-2:issues',
          connector_type: 'jira',
          resource: 'issues',
          sample_count: 0,
          status: 'unavailable',
          error: 'connector query timed out',
        },
      ],
    },
    review_state: 'needs_review',
    activation_status: 'not_activated',
    observed_facts: [
      {
        id: OBSERVED_ID,
        statement: 'Observed 3 records from repos',
        kind: 'observed',
        confidence: 'high',
        evidence_refs: [ref('evidence:c-1:repos', 'c-1:repos', 'scan:repos')],
      },
    ],
    derived_elements: [
      {
        id: DERIVED_A_ID,
        statement: 'Development stage detected',
        kind: 'derived',
        confidence: 'low',
        evidence_refs: [
          ref('evidence:c-1:repos', 'c-1:repos', 'scan:repos'),
          ref('evidence:c-2:issues', 'c-2:issues', 'scan:issues', 'derived'),
        ],
      },
      {
        id: DERIVED_B_ID,
        statement: 'Release automation detected',
        kind: 'derived',
        confidence: 'medium',
        evidence_refs: [ref('evidence:c-1:workflows', 'c-1:workflows', 'scan:workflows')],
      },
    ],
    unresolved_assumptions: [
      {
        id: ASSUMPTION_ID,
        statement: 'Status taxonomy varies by tool',
        reason: 'Insufficient evidence to resolve the claim.',
        confidence: 'low',
        evidence_refs: [],
      },
    ],
    unavailable_evidence: [
      {
        source_id: 'c-2:issues',
        reason: 'connector query timed out',
        requested_resource: 'issues',
      },
    ],
    confidence: 'high',
    uncertainty: ['Deployment stage cannot be assessed without a deployment tool connector'],
    provenance: [ref('evidence:c-1:repos', 'c-1:repos', 'scan:repos')],
  } as DeterminationProposal
}

function review(): DeterminationReviewResponse {
  return {
    proposal: { ...proposal(), review_state: 'approved' },
    decision: 'approve',
    review_state: 'approved',
    activation_status: 'not_activated',
    actor_id: '77777777-7777-4777-8777-777777777777',
    actor_username: 'dana',
    decided_at: '2026-01-02T03:04:05Z',
    reason: null,
    acknowledged_assumption_ids: [],
    corrected_derived_element_ids: [],
    removed_derived_element_ids: [],
  } as DeterminationReviewResponse
}

function mountPanel(options: {
  proposal?: DeterminationProposal
  review?: DeterminationReviewResponse | null
} = {}) {
  return mount(DeterminationProposalPanel, {
    props: {
      proposal: options.proposal ?? proposal(),
      review: options.review ?? null,
      loading: false,
      error: null,
    },
  })
}

async function openEvidence(
  wrapper: ReturnType<typeof mountPanel>,
  rowTestid: string,
  rowIndex: number,
) {
  const row = wrapper.findAll(`[data-testid="${rowTestid}"]`)[rowIndex]
  expect(row, `row ${rowIndex} of ${rowTestid} must exist`).toBeDefined()
  await row.find('[data-testid="determination-element-evidence"]').trigger('click')
  await flushPromises()
  return wrapper.find('[data-testid="determination-evidence-panel"]')
}

describe('evidencePresentation', () => {
  it('keeps observed, derived/inferred and unavailable visually distinct', () => {
    expect(provenanceBadgeClass('observed')).toContain('success')
    expect(provenanceBadgeClass('derived')).toContain('warning')
    expect(provenanceBadgeClass('inferred')).toContain('warning')
    expect(provenanceBadgeClass('unavailable')).toContain('destructive')
    // An out-of-vocabulary value still gets a stable neutral class.
    expect(provenanceBadgeClass('something-else')).toContain('muted')

    expect(elementKindBadgeClass('observed')).toContain('success')
    expect(elementKindBadgeClass('derived')).toContain('warning')
    expect(elementKindBadgeClass('assumption')).toContain('muted')
  })

  it('withholds credential-shaped values but keeps ordinary locators readable', () => {
    expect(looksLikeSecret('token=ghp_abcdefghijklmnopqrstuvwxyz012345')).toBe(true)
    expect(looksLikeSecret('sk-abcdefghijklmnopqrstuvwx')).toBe(true)
    expect(looksLikeSecret('https://user:hunter2@example.test/repos')).toBe(true)
    expect(looksLikeSecret('Authorization: Bearer abcDEF123456ghijkl')).toBe(true)
    expect(looksLikeSecret('-----BEGIN RSA PRIVATE KEY-----')).toBe(true)
    // A commit id is a locator, not a credential: hiding it would make the
    // evidence unreviewable.
    expect(looksLikeSecret('commit:3f4a9c2b1d0e5f6a7b8c9d0e1f2a3b4c5d6e7f80')).toBe(false)
    expect(looksLikeSecret('repo:farnalabs/modulo#3050')).toBe(false)
    expect(looksLikeSecret('scan:repos')).toBe(false)

    expect(displayEvidenceText(SECRET_LOCATOR)).toEqual({ withheld: true, text: '' })
    expect(displayEvidenceText('scan:repos')).toEqual({ withheld: false, text: 'scan:repos' })
  })

  it('detects long high-entropy blobs but not hex digests or plain words', () => {
    // Long, non-hex and mixed upper/lower/digit: a bare token.
    expect(looksLikeSecret('AbCdEf0123456789abcdefghijABCDEF0123456789')).toBe(true)
    // Each is long, non-hex and lacks one entropy class, so none is a token.
    expect(looksLikeSecret('abcdefghijklmnopqrstuvwxyz0123456789abcdef')).toBe(false)
    expect(looksLikeSecret('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ABCDEF')).toBe(false)
    expect(looksLikeSecret('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRST')).toBe(false)
    // A 40-char hex digest is a locator, not a credential.
    expect(looksLikeSecret('0123456789abcdef0123456789abcdef01234567')).toBe(false)
  })

  it('withholds a bare bearer token that has no assignment prefix', () => {
    expect(looksLikeSecret('Bearer abcDEF123456ghijklmnop')).toBe(true)
    expect(displayEvidenceText('Bearer abcDEF123456ghijklmnop')).toEqual({
      withheld: true,
      text: '',
    })
  })

  it('keys unavailable evidence by its own content, never by position', () => {
    const first = { source_id: 'c-2:issues', requested_resource: 'issues', reason: 'timeout' }
    const second = { source_id: 'c-2:issues', requested_resource: 'issues', reason: 'not found' }
    expect(unavailableEvidenceKey(first)).toBe(unavailableEvidenceKey({ ...first }))
    expect(unavailableEvidenceKey(first)).not.toBe(unavailableEvidenceKey(second))
  })
})

describe('Determination evidence drill-down', () => {
  it('opens a well-supported observed element with its references and provenance', async () => {
    const wrapper = mountPanel()

    const panel = await openEvidence(wrapper, 'determination-observed-fact', 0)

    expect(panel.exists()).toBe(true)
    expect(panel.find('[data-testid="determination-evidence-panel-statement"]').text()).toBe(
      'Observed 3 records from repos',
    )
    expect(panel.find('[data-testid="determination-evidence-panel-kind"]').text()).toBe('Observed')
    expect(panel.find('[data-testid="determination-evidence-panel-confidence"]').text()).toBe('High')

    const refs = panel.findAll('[data-testid="determination-evidence-ref"]')
    expect(refs).toHaveLength(1)
    expect(refs[0].text()).toContain('evidence:c-1:repos')
    expect(refs[0].text()).toContain('c-1:repos')
    expect(refs[0].text()).toContain('scan:repos')
    expect(refs[0].find('[data-testid="determination-evidence-provenance"]').text()).toBe('Observed')
  })

  it('shows a low-confidence element with its own uncertainty and marks it derived', async () => {
    const wrapper = mountPanel()

    const panel = await openEvidence(wrapper, 'determination-assumption', 0)

    expect(panel.find('[data-testid="determination-evidence-panel-kind"]').text()).toBe('Unresolved assumption')
    const confidence = panel.find('[data-testid="determination-evidence-panel-confidence"]')
    expect(confidence.text()).toBe('Low')
    expect(confidence.classes().join(' ')).toContain('destructive')
    expect(panel.find('[data-testid="determination-evidence-panel-uncertainty"]').text()).toBe(
      'Insufficient evidence to resolve the claim.',
    )
  })

  it('lists every reference of a multi-source element with its recorded provenance', async () => {
    const wrapper = mountPanel()

    const panel = await openEvidence(wrapper, 'determination-derived-element', 0)

    const refs = panel.findAll('[data-testid="determination-evidence-ref"]')
    expect(refs).toHaveLength(2)
    const provenances = refs.map((entry) =>
      entry.find('[data-testid="determination-evidence-provenance"]').text(),
    )
    expect(provenances).toEqual(['Observed', 'Derived'])
    expect(panel.text()).toContain('c-1:repos')
    expect(panel.text()).toContain('c-2:issues')
  })

  it('states explicitly that an element has no supporting evidence', async () => {
    const wrapper = mountPanel()

    const panel = await openEvidence(wrapper, 'determination-assumption', 0)

    const empty = panel.find('[data-testid="determination-evidence-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toBe('No supporting evidence.')
    expect(panel.findAll('[data-testid="determination-evidence-ref"]')).toHaveLength(0)
  })

  it('reflects a corrected element without moving another element’s references', async () => {
    const wrapper = mountPanel()

    // Select the second derived element and prove it is the one on screen.
    const panel = await openEvidence(wrapper, 'determination-derived-element', 1)
    expect(panel.find('[data-testid="determination-evidence-panel-statement"]').text()).toBe(
      'Release automation detected',
    )
    const referenceIds = () =>
      wrapper
        .find('[data-testid="determination-evidence-panel"]')
        .findAll('[data-testid="determination-evidence-ref"]')
        .map((entry) => entry.text())
    expect(referenceIds()[0]).toContain('evidence:c-1:workflows')

    // Remove the FIRST derived element and correct the second one's statement.
    const next = proposal()
    next.derived_elements = [
      {
        ...next.derived_elements![1],
        statement: 'Release automation detected in workflows',
      },
    ]
    await wrapper.setProps({ proposal: next })
    await flushPromises()

    const reopened = wrapper.find('[data-testid="determination-evidence-panel"]')
    expect(reopened.exists()).toBe(true)
    expect(reopened.find('[data-testid="determination-evidence-panel-statement"]').text()).toBe(
      'Release automation detected in workflows',
    )
    // The untouched element keeps its own reference, keyed by id - nothing was
    // renumbered or re-pointed at the surviving element's neighbour.
    expect(referenceIds()[0]).toContain('evidence:c-1:workflows')
    expect(reopened.find('[data-testid="determination-evidence-panel-kind"]').text()).toBe('Derived')
  })

  it('closes on Escape and on the Close button', async () => {
    const wrapper = mountPanel()

    await openEvidence(wrapper, 'determination-observed-fact', 0)
    await wrapper.find('[data-testid="determination-evidence-panel"]').trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(wrapper.find('[data-testid="determination-evidence-panel"]').exists()).toBe(false)

    await openEvidence(wrapper, 'determination-observed-fact', 0)
    await wrapper.find('[data-testid="determination-evidence-panel-close"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="determination-evidence-panel"]').exists()).toBe(false)
  })

  it('never renders a credential-shaped locator, but shows the field as withheld', async () => {
    const next = proposal()
    next.observed_facts = [
      {
        ...next.observed_facts![0],
        evidence_refs: [ref('evidence:c-9', 'c-9:repos', SECRET_LOCATOR)],
      },
    ]
    const wrapper = mountPanel({ proposal: next })

    const panel = await openEvidence(wrapper, 'determination-observed-fact', 0)

    expect(panel.find('[data-testid="determination-evidence-withheld"]').text()).toContain('withheld')
    expect(panel.text()).not.toContain('ghp_abcdefghijklmnopqrstuvwxyz012345')
    // The reference itself is still listed - only the value is withheld.
    expect(panel.text()).toContain('evidence:c-9')
    expect(wrapper.html()).not.toContain('ghp_abcdefghijklmnopqrstuvwxyz012345')
  })

  it('withholds a credential-shaped uncertainty instead of rendering it', async () => {
    const next = proposal()
    next.unresolved_assumptions = [
      {
        ...next.unresolved_assumptions![0],
        reason: 'api_key=ghp_abcdefghijklmnopqrstuvwxyz012345',
      },
    ]
    const wrapper = mountPanel({ proposal: next })

    const panel = await openEvidence(wrapper, 'determination-assumption', 0)

    expect(
      panel.find('[data-testid="determination-evidence-panel-uncertainty-withheld"]').exists(),
    ).toBe(true)
    expect(wrapper.html()).not.toContain('ghp_abcdefghijklmnopqrstuvwxyz012345')
  })

  it('closes on a backdrop click but not on a click inside the panel', async () => {
    const wrapper = mountPanel()

    await openEvidence(wrapper, 'determination-observed-fact', 0)

    // A click on a child bubbles to the dialog with a different target: keep open.
    await wrapper.find('[data-testid="determination-evidence-panel-statement"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="determination-evidence-panel"]').exists()).toBe(true)

    // A click on the dialog root itself is a backdrop click: close.
    await wrapper.find('[data-testid="determination-evidence-panel"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="determination-evidence-panel"]').exists()).toBe(false)
  })

  it('emits close for a native close while selected and tolerates a second close request', async () => {
    const element: EvidenceElementView = {
      id: 'e-1',
      kind: 'observed',
      statement: 'Observed record',
      confidence: 'high',
      evidenceRefs: [],
      uncertainty: null,
    }
    const wrapper = mount(DeterminationEvidencePanel, { props: { element } })
    await flushPromises()

    const dialog = wrapper.find('dialog')
    expect((dialog.element as HTMLDialogElement).open).toBe(true)

    // A browser-native close (not routed through requestClose) still emits.
    ;(dialog.element as HTMLDialogElement).close()
    await vi.waitFor(() => expect(wrapper.emitted('close')).toHaveLength(1))

    // requestClose on an already-closed dialog must not throw and still emits.
    await wrapper.find('[data-testid="determination-evidence-panel-close"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(2)
  })

  it('closes the drill-down when the selected element disappears from the proposal', async () => {
    const wrapper = mountPanel()
    await openEvidence(wrapper, 'determination-derived-element', 0)
    expect(wrapper.find('[data-testid="determination-evidence-panel"]').exists()).toBe(true)

    const next = proposal()
    next.derived_elements = []
    await wrapper.setProps({ proposal: next })
    await flushPromises()

    expect(wrapper.find('[data-testid="determination-evidence-panel"]').exists()).toBe(false)
  })

  it('opens an element that has no evidence_refs field with an explicit empty state', async () => {
    const next = proposal()
    next.derived_elements = [
      { id: 'dddd', statement: 'No references', kind: 'derived', confidence: 'high' },
    ] as DeterminationProposal['derived_elements']
    const wrapper = mountPanel({ proposal: next })

    const panel = await openEvidence(wrapper, 'determination-derived-element', 0)

    expect(panel.find('[data-testid="determination-evidence-empty"]').exists()).toBe(true)
  })
})

describe('Determination proposal panel states', () => {
  it('renders the loading state', () => {
    const wrapper = mount(DeterminationProposalPanel, {
      props: { proposal: null, loading: true, error: null },
    })

    expect(wrapper.find('[data-testid="determination-proposal-loading"]').exists()).toBe(true)
  })

  it('renders the empty state when no proposal, no error and not loading', () => {
    const wrapper = mount(DeterminationProposalPanel, {
      props: { proposal: null, loading: false, error: null },
    })

    expect(wrapper.find('[data-testid="determination-proposal-empty"]').exists()).toBe(true)
  })

  it('withholds a credential-shaped unavailable-evidence reason', () => {
    const next = proposal()
    next.unavailable_evidence = [
      {
        source_id: 'c-2:issues',
        requested_resource: 'issues',
        reason: 'token=ghp_abcdefghijklmnopqrstuvwxyz012345',
      },
    ]
    const wrapper = mountPanel({ proposal: next })

    const row = wrapper.find('[data-testid="determination-unavailable-row"]')
    expect(row.find('[data-testid="determination-evidence-withheld"]').exists()).toBe(true)
    expect(wrapper.html()).not.toContain('ghp_abcdefghijklmnopqrstuvwxyz012345')
  })
})

describe('Determination provenance trail', () => {
  it('renders the attempt, contract, confidence, uncertainty and full source inventory', () => {
    const wrapper = mountPanel()

    const trail = wrapper.find('[data-testid="determination-provenance-trail"]')
    expect(trail.exists()).toBe(true)
    expect(trail.find('[data-testid="determination-provenance-attempt-id"]').text()).toBe(ATTEMPT_ID)
    expect(trail.find('[data-testid="determination-provenance-attempt-status"]').text()).toBe('Completed with gaps')
    expect(trail.find('[data-testid="determination-provenance-contract-version"]').text()).toBe(
      'alpha.determination-proposal.v1',
    )
    expect(trail.find('[data-testid="determination-proposal-confidence"]').text()).toBe('High')
    expect(trail.text()).toContain(
      'Deployment stage cannot be assessed without a deployment tool connector',
    )

    const sources = trail.findAll('[data-testid="determination-source-row"]')
    expect(sources).toHaveLength(2)
    expect(sources[0].text()).toContain('github')
    expect(sources[0].text()).toContain('repos')
    expect(sources[0].text()).toContain('3')
    expect(sources[0].text()).toContain('Available')
    expect(sources[1].text()).toContain('Unavailable')
    expect(sources[1].find('[data-testid="determination-source-error"]').text()).toContain(
      'connector query timed out',
    )
    expect(trail.findAll('[data-testid="determination-provenance-row"]')).toHaveLength(1)
  })

  it('states that no decision has been recorded yet', () => {
    const wrapper = mountPanel()

    expect(wrapper.find('[data-testid="determination-provenance-decision-empty"]').text()).toBe(
      'No review decision has been recorded for this proposal.',
    )
    expect(wrapper.find('[data-testid="determination-provenance-decision-state"]').exists()).toBe(false)
  })

  it('renders the decision record once a review decision exists', () => {
    const wrapper = mountPanel({ review: review() })

    expect(wrapper.find('[data-testid="determination-provenance-decision-empty"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-provenance-decision-state"]').text()).toBe('Approved')
    expect(wrapper.find('[data-testid="determination-provenance-decision-actor"]').text()).toBe('dana')
    expect(
      wrapper.find('[data-testid="determination-provenance-decision-decided-at"]').text().length,
    ).toBeGreaterThan(0)
  })

  it('renders unavailable evidence as a first-class section with its reason and resource', () => {
    const wrapper = mountPanel()

    const unavailable = wrapper.findAll('[data-testid="determination-unavailable-row"]')
    expect(unavailable).toHaveLength(1)
    expect(unavailable[0].text()).toContain('c-2:issues')
    expect(unavailable[0].text()).toContain('issues')
    expect(unavailable[0].text()).toContain('connector query timed out')
  })

  it('withholds credential-shaped provenance locators and source errors', () => {
    const next = proposal()
    next.provenance = [ref('evidence:c-9', 'c-9', 'token=ghp_abcdefghijklmnopqrstuvwxyz012345')]
    next.attempt = {
      ...next.attempt,
      source_inventory: [
        {
          source_id: 'c-9:repos',
          connector_type: 'github',
          resource: 'repos',
          sample_count: 0,
          status: 'unavailable',
          error: 'api_key=ghp_abcdefghijklmnopqrstuvwxyz012345',
        },
      ],
    }
    const wrapper = mountPanel({ proposal: next })

    const provenanceRow = wrapper.find('[data-testid="determination-provenance-row"]')
    expect(provenanceRow.find('[data-testid="determination-evidence-withheld"]').exists()).toBe(true)

    const sourceRow = wrapper.find('[data-testid="determination-source-row"]')
    expect(sourceRow.find('[data-testid="determination-source-error"]').text()).toContain('withheld')
    expect(wrapper.html()).not.toContain('ghp_abcdefghijklmnopqrstuvwxyz012345')
  })

  it('withholds a credential-shaped connector error in the evidence drill-down', async () => {
    const next = proposal()
    const derived = next.derived_elements ?? []
    derived[0].evidence_refs = [ref('evidence:c-9:repos', 'c-9:repos', 'scan:repos')]
    next.attempt = {
      ...next.attempt,
      source_inventory: [
        {
          source_id: 'c-9:repos',
          connector_type: 'github',
          resource: 'repos',
          sample_count: 0,
          status: 'unavailable',
          error: 'api_key=ghp_abcdefghijklmnopqrstuvwxyz012345',
        },
      ],
    }
    const wrapper = mountPanel({ proposal: next })

    const toggle = wrapper.find('[data-testid="determination-derived-element"] [data-testid="determination-evidence-toggle"]')
    expect(toggle.exists()).toBe(true)
    await toggle.trigger('click')

    const drilldown = wrapper.find('[data-testid="determination-derived-element"] [data-testid="determination-evidence-drilldown"]')
    expect(drilldown.exists()).toBe(true)
    expect(drilldown.find('[data-testid="determination-evidence-withheld"]').text()).toContain('withheld')
    expect(wrapper.html()).not.toContain('ghp_abcdefghijklmnopqrstuvwxyz012345')
  })

  it('marks every proposal element with its kind so an inference is never shown as observed', () => {
    const wrapper = mountPanel()

    const kinds = wrapper.findAll('[data-testid="determination-element-kind"]').map((entry) => entry.text())
    expect(kinds).toEqual(['Observed', 'Derived', 'Derived', 'Unresolved assumption'])
    // Each element also offers its own drill-down control.
    expect(wrapper.findAll('[data-testid="determination-element-evidence"]')).toHaveLength(4)
  })
})
