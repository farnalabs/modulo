import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import DeterminationReviewPanel from '../../components/determination/DeterminationReviewPanel.vue'

const PROPOSAL_ID = '11111111-1111-4111-8111-111111111111'
const DERIVED_ID = '33333333-3333-4333-8333-333333333333'
const SECOND_DERIVED_ID = '88888888-8888-4888-8888-888888888888'
const LOW_ASSUMPTION_ID = '44444444-4444-4444-8444-444444444444'
const MEDIUM_ASSUMPTION_ID = '99999999-9999-4999-8999-999999999999'

function proposal() {
  return {
    contract_version: 'alpha.determination-proposal.v1' as const,
    proposal_id: PROPOSAL_ID,
    attempt: {
      id: '66666666-6666-4666-8666-666666666666',
      status: 'completed_with_gaps' as const,
      source_inventory: [
        {
          source_id: 'c-1:repos',
          connector_type: 'github',
          resource: 'repos',
          sample_count: 3,
          status: 'available' as const,
          error: null,
        },
      ],
    },
    review_state: 'needs_review' as const,
    activation_status: 'not_activated' as const,
    observed_facts: [],
    derived_elements: [
      {
        id: DERIVED_ID,
        statement: 'Development stage detected',
        kind: 'derived' as const,
        confidence: 'high' as const,
        evidence_refs: [],
      },
      {
        id: SECOND_DERIVED_ID,
        statement: 'Code review stage detected',
        kind: 'derived' as const,
        confidence: 'medium' as const,
        evidence_refs: [],
      },
    ],
    unresolved_assumptions: [
      {
        id: LOW_ASSUMPTION_ID,
        statement: 'Status taxonomy varies by tool',
        reason: 'Insufficient evidence.',
        confidence: 'low' as const,
        evidence_refs: [],
      },
      {
        id: MEDIUM_ASSUMPTION_ID,
        statement: 'Deployment inferred from metadata',
        reason: 'Observed only.',
        confidence: 'medium' as const,
        evidence_refs: [],
      },
    ],
    unavailable_evidence: [],
    confidence: 'high' as const,
    uncertainty: [],
    provenance: [],
  }
}

function mountPanel() {
  return mount(DeterminationReviewPanel, {
    props: { proposal: proposal(), review: null, loading: false, error: null },
  })
}

describe('DeterminationReviewPanel', () => {
  it('blocks approve until all blocking assumptions are acknowledged', async () => {
    const wrapper = mountPanel()
    const approve = wrapper.find('[data-testid="determination-review-approve"]')

    expect(approve.attributes('disabled')).toBeDefined()
    // medium confidence is not blocking, so it has no acknowledgement control.
    expect(wrapper.find(`[data-testid="determination-review-acknowledge-${MEDIUM_ASSUMPTION_ID}"]`).exists()).toBe(false)

    await wrapper.find(`[data-testid="determination-review-acknowledge-${LOW_ASSUMPTION_ID}"]`).setValue(true)
    expect(approve.attributes('disabled')).toBeUndefined()

    await approve.trigger('click')

    const submissions = wrapper.emitted('submit')
    expect(submissions).toHaveLength(1)
    expect(submissions?.[0]?.[0]).toMatchObject({
      decision: 'approve',
      acknowledgedAssumptionIds: [LOW_ASSUMPTION_ID],
      correctedDerivedElements: [],
      removedDerivedElementIds: [],
    })
  })

  it('refuses a reject without a reason and then emits it with the reason', async () => {
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="determination-review-reject"]').trigger('click')

    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.find('[data-testid="determination-review-reason-required"]').exists()).toBe(true)

    await wrapper.find('[data-testid="determination-review-reason"]').setValue('  Out of date  ')
    await wrapper.find('[data-testid="determination-review-reject"]').trigger('click')

    const submissions = wrapper.emitted('submit')
    expect(submissions?.[0]?.[0]).toMatchObject({ decision: 'reject', reason: 'Out of date' })
    expect(wrapper.find('[data-testid="determination-review-reason-required"]').exists()).toBe(false)
  })

  it('refuses every decision while a derived statement is blank', async () => {
    const wrapper = mountPanel()

    await wrapper.find(`[data-testid="determination-review-statement-${DERIVED_ID}"]`).setValue('   ')

    expect(wrapper.find('[data-testid="determination-review-statements-invalid"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="determination-review-approve"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="determination-review-needs-review"]').attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="determination-review-needs-review"]').trigger('click')
    expect(wrapper.emitted('submit')).toBeUndefined()
  })

  it('sends a removed element by id until it is restored', async () => {
    const wrapper = mountPanel()

    await wrapper.find(`[data-testid="determination-review-remove-${DERIVED_ID}"]`).trigger('click')
    await wrapper.find('[data-testid="determination-review-needs-review"]').trigger('click')

    expect(wrapper.emitted('submit')?.[0]?.[0]).toMatchObject({
      removedDerivedElementIds: [DERIVED_ID],
      correctedDerivedElements: [],
    })

    // Restoring the element removes it from the removal list again.
    const restore = wrapper.find(`[data-testid="determination-review-remove-${DERIVED_ID}"]`)
    expect(restore.attributes('aria-pressed')).toBe('true')
    await restore.trigger('click')
    await wrapper.find('[data-testid="determination-review-needs-review"]').trigger('click')

    expect(wrapper.emitted('submit')?.[1]?.[0]).toMatchObject({
      removedDerivedElementIds: [],
      correctedDerivedElements: [],
    })
  })

  it('sends a correction only when the value actually changed', async () => {
    const wrapper = mountPanel()

    await wrapper.find(`[data-testid="determination-review-confidence-${SECOND_DERIVED_ID}"]`).setValue('high')
    await wrapper.find('[data-testid="determination-review-needs-review"]').trigger('click')

    expect(wrapper.emitted('submit')?.[0]?.[0]).toMatchObject({
      correctedDerivedElements: [{ id: SECOND_DERIVED_ID, confidence: 'high' }],
    })

    // Reverting the confidence leaves no correction for that element at all.
    await wrapper.find(`[data-testid="determination-review-confidence-${SECOND_DERIVED_ID}"]`).setValue('medium')
    await wrapper.find('[data-testid="determination-review-needs-review"]').trigger('click')

    expect(wrapper.emitted('submit')?.[1]?.[0]).toMatchObject({ correctedDerivedElements: [] })
  })
})
