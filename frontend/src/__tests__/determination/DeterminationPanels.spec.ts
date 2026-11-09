import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import DeterminationScanPanel from '../../components/determination/DeterminationScanPanel.vue'
import DeterminationProposalPanel from '../../components/determination/DeterminationProposalPanel.vue'
import DeterminationReviewPanel from '../../components/determination/DeterminationReviewPanel.vue'
import {
  confidenceBadgeClass,
  isUnsettledConfidence,
} from '../../components/determination/confidencePresentation'
import type {
  DeterminationProposal,
  DeterminationReviewResponse,
  DeterminationScanResponse,
} from '../../composables/useDetermination'

const PROPOSAL_ID = '11111111-1111-4111-8111-111111111111'
const ATTEMPT_ID = '66666666-6666-4666-8666-666666666666'
const DERIVED_ID = '33333333-3333-4333-8333-333333333333'
const ASSUMPTION_ID = '44444444-4444-4444-8444-444444444444'

function baseProposal(): DeterminationProposal {
  return {
    contract_version: 'alpha.determination-proposal.v1',
    proposal_id: PROPOSAL_ID,
    attempt: {
      id: ATTEMPT_ID,
      status: 'completed',
      source_inventory: [],
    },
    review_state: 'needs_review',
    activation_status: 'not_activated',
    observed_facts: [],
    derived_elements: [
      {
        id: DERIVED_ID,
        statement: 'Development stage detected',
        kind: 'derived',
        confidence: 'high',
        evidence_refs: [],
      },
    ],
    unresolved_assumptions: [
      {
        id: ASSUMPTION_ID,
        statement: 'Status taxonomy varies by tool',
        reason: 'Insufficient evidence.',
        confidence: 'low',
        evidence_refs: [],
      },
    ],
    unavailable_evidence: [],
    confidence: 'high',
    uncertainty: [],
    provenance: [],
  } as DeterminationProposal
}

function reviewPayload(): DeterminationReviewResponse {
  return {
    proposal: baseProposal(),
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

function scanPayload(): DeterminationScanResponse {
  return {
    samples: [],
    findings: [],
    summary: 'SDLC stages detected: development',
  } as DeterminationScanResponse
}

describe('confidencePresentation', () => {
  it('maps every confidence value to a badge class and flags unsettled values', () => {
    expect(confidenceBadgeClass('high')).toContain('success')
    expect(confidenceBadgeClass('medium')).toContain('warning')
    expect(confidenceBadgeClass('low')).toContain('destructive')
    expect(confidenceBadgeClass('unknown')).toContain('muted')
    // An out-of-vocabulary value still gets a stable neutral class.
    expect(confidenceBadgeClass('something-else')).toContain('muted')

    expect(isUnsettledConfidence('low')).toBe(true)
    expect(isUnsettledConfidence('unknown')).toBe(true)
    expect(isUnsettledConfidence('high')).toBe(false)
  })
})

describe('DeterminationScanPanel', () => {
  it('renders the empty sample and finding states', () => {
    const wrapper = mount(DeterminationScanPanel, {
      props: { scan: scanPayload(), loading: false, error: null },
    })

    expect(wrapper.find('[data-testid="determination-samples-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="determination-findings-empty"]').exists()).toBe(true)
  })

  it('shows the error and re-emits retry', async () => {
    const wrapper = mount(DeterminationScanPanel, {
      props: { scan: null, loading: false, error: 'scan exploded' },
    })

    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })
})

describe('DeterminationProposalPanel', () => {
  it('renders the empty sections when the proposal omits its collections', () => {
    const partial = {
      ...baseProposal(),
      observed_facts: undefined,
      derived_elements: undefined,
      unresolved_assumptions: undefined,
      unavailable_evidence: undefined,
      provenance: undefined,
      uncertainty: undefined,
      attempt: { id: ATTEMPT_ID, status: 'completed', source_inventory: undefined },
    } as unknown as DeterminationProposal

    const wrapper = mount(DeterminationProposalPanel, {
      props: { proposal: partial, loading: false, error: null },
    })

    expect(wrapper.find('[data-testid="determination-proposal-not-activated"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-testid="determination-observed-fact"]')).toHaveLength(0)
    expect(wrapper.findAll('[data-testid="determination-derived-element"]')).toHaveLength(0)
    expect(wrapper.findAll('[data-testid="determination-assumption"]')).toHaveLength(0)
  })

  it('treats missing evidence references as empty', () => {
    const populated = {
      ...baseProposal(),
      observed_facts: [
        { id: 'aaaa', statement: 'observed', kind: 'observed', confidence: 'high' },
      ],
      derived_elements: [
        { id: 'bbbb', statement: 'derived', kind: 'derived', confidence: 'medium' },
      ],
      unresolved_assumptions: [
        { id: 'cccc', statement: 'assumption', reason: 'reason', confidence: 'low' },
      ],
    } as unknown as DeterminationProposal

    const wrapper = mount(DeterminationProposalPanel, {
      props: { proposal: populated, loading: false, error: null },
    })

    expect(wrapper.findAll('[data-testid="determination-observed-fact"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-derived-element"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-assumption"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-evidence-empty"]')).toHaveLength(3)
  })

  it('shows the error and re-emits retry', async () => {
    const wrapper = mount(DeterminationProposalPanel, {
      props: { proposal: null, loading: false, error: 'draft exploded' },
    })

    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })
})

describe('DeterminationReviewPanel', () => {
  it('renders the empty derived and blocking states', () => {
    const partial = {
      ...baseProposal(),
      derived_elements: undefined,
      unresolved_assumptions: undefined,
    } as unknown as DeterminationProposal

    const wrapper = mount(DeterminationReviewPanel, {
      props: { proposal: partial, review: null, loading: false, error: null },
    })

    expect(wrapper.find('[data-testid="determination-review-derived-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="determination-review-blocking-empty"]').exists()).toBe(true)
  })

  it('renders a recorded decision', () => {
    const wrapper = mount(DeterminationReviewPanel, {
      props: { proposal: baseProposal(), review: reviewPayload(), loading: false, error: null },
    })

    expect(wrapper.find('[data-testid="determination-review-result-state"]').text()).toBe('Approved')
    expect(wrapper.find('[data-testid="determination-review-result-actor"]').text()).toBe('dana')
  })

  it('skips a derived element that has no draft when submitting', async () => {
    const proposal = baseProposal()
    const wrapper = mount(DeterminationReviewPanel, {
      props: { proposal, review: null, loading: false, error: null },
    })

    const elements = proposal.derived_elements!
    elements.push({
      id: 'eeee',
      statement: 'Undrafted',
      kind: 'derived',
      confidence: 'high',
      evidence_refs: [],
    } as unknown as (typeof elements)[number])

    await wrapper.find('[data-testid="determination-review-needs-review"]').trigger('click')

    const submissions = wrapper.emitted('submit')
    expect(submissions).toHaveLength(1)
    expect(submissions?.[0]?.[0]).toMatchObject({ correctedDerivedElements: [] })
  })
})
