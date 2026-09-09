import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import HitlBriefing from '../../components/HitlBriefing.vue'

const fullContext = {
  description: 'Review the generated comments before posting to the PR.',
  condition: "node_id=='550e8400-e29b-41d4-a716-446655440000'",
  condition_result: {
    expression: "node_id=='550e8400-e29b-41d4-a716-446655440000'",
    value: '{"comment":"ship it"}',
    evaluated_at_node: '550e8400-e29b-41d4-a716-446655440000',
  },
  trigger: 'condition',
  source_node_id: '550e8400-e29b-41d4-a716-446655440000',
  source_node_label: 'Comment Generator',
  artifacts: [{ node_id: '550e8400-e29b-41d4-a716-446655440000', summary: '{"comment":"Looks good"}' }],
  reason: null,
  pipeline_name: 'PR Reviewer',
}

describe('HitlBriefing', () => {
  it('renders the description always visible', () => {
    const wrapper = mount(HitlBriefing, {
      props: { description: 'Approve only if the diff is covered by tests.', context: null },
    })
    const description = wrapper.find('[data-testid="hitl-briefing-description"]')
    expect(description.exists()).toBe(true)
    expect(description.text()).toBe('Approve only if the diff is covered by tests.')
    expect(wrapper.find('[data-testid="hitl-briefing-details"]').exists()).toBe(false)
  })

  it('renders the muted fallback for a legacy gate without description', () => {
    const wrapper = mount(HitlBriefing, { props: { description: null, context: null } })
    const fallback = wrapper.find('[data-testid="hitl-briefing-description-fallback"]')
    expect(fallback.exists()).toBe(true)
    expect(fallback.text()).toContain('No description provided for this gate')
    expect(wrapper.find('[data-testid="hitl-briefing-description"]').exists()).toBe(false)
  })

  it('treats a whitespace-only description as missing', () => {
    const wrapper = mount(HitlBriefing, { props: { description: '   ', context: null } })
    expect(wrapper.find('[data-testid="hitl-briefing-description-fallback"]').exists()).toBe(true)
  })

  it('shows the details toggle when context carries fire-time data', async () => {
    const wrapper = mount(HitlBriefing, {
      props: { description: 'Why this gate exists — with context.', context: fullContext },
    })
    const toggle = wrapper.find('[data-testid="hitl-briefing-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[data-testid="hitl-briefing-details"]').exists()).toBe(false)

    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    const details = wrapper.find('[data-testid="hitl-briefing-details"]')
    expect(details.exists()).toBe(true)
    expect(details.text()).toContain('Fired by a condition on the edge')
    expect(details.text()).toContain('PR Reviewer')
    expect(details.text()).toContain('Comment Generator')
    expect(details.text()).toContain("node_id=='550e8400-e29b-41d4-a716-446655440000'")
  })

  it('renders the node-gate reason in the details', async () => {
    const wrapper = mount(HitlBriefing, {
      props: {
        description: 'Human confirms the incident resolution.',
        context: { ...fullContext, trigger: 'node', condition: null, reason: 'Two failed escalations in a row.' },
      },
    })
    await wrapper.find('[data-testid="hitl-briefing-toggle"]').trigger('click')
    const details = wrapper.find('[data-testid="hitl-briefing-details"]')
    expect(details.text()).toContain('Raised by a HITL node')
    expect(details.text()).toContain('Two failed escalations in a row.')
  })

  it('renders the matched condition value as primary evidence (FAR-688)', async () => {
    const wrapper = mount(HitlBriefing, {
      props: { description: 'Why this gate exists.', context: fullContext },
    })
    await wrapper.find('[data-testid="hitl-briefing-toggle"]').trigger('click')
    const matched = wrapper.find('[data-testid="hitl-briefing-condition-result"]')
    expect(matched.exists()).toBe(true)
    expect(matched.text()).toContain('Condition evaluated to')
    expect(matched.text()).toContain('{"comment":"ship it"}')
  })

  it('hides the matched-value block for a legacy payload without condition_result', async () => {
    const wrapper = mount(HitlBriefing, {
      props: { description: 'Why this gate exists.', context: { ...fullContext, condition_result: null } },
    })
    await wrapper.find('[data-testid="hitl-briefing-toggle"]').trigger('click')
    expect(wrapper.find('[data-testid="hitl-briefing-condition-result"]').exists()).toBe(false)
    // The supplementary artifacts still render (existing behaviour unchanged).
    expect(wrapper.text()).toContain('{"comment":"Looks good"}')
  })

  it('renders a truncated artifact summary verbatim with its marker', async () => {
    const wrapper = mount(HitlBriefing, {
      props: {
        description: 'Why this gate exists.',
        context: {
          ...fullContext,
          artifacts: [{ node_id: '550e8400-e29b-41d4-a716-446655440000', summary: '{"blob":"xxxx…(truncated)"}' }],
        },
      },
    })
    await wrapper.find('[data-testid="hitl-briefing-toggle"]').trigger('click')
    const details = wrapper.find('[data-testid="hitl-briefing-details"]')
    expect(details.text()).toContain('{"blob":"xxxx…(truncated)"}')
  })

  it('renders two artifacts sharing one node_id without duplicate keys (FAR-688)', async () => {
    const wrapper = mount(HitlBriefing, {
      props: {
        description: 'Why this gate exists.',
        context: {
          ...fullContext,
          artifacts: [
            { node_id: '550e8400-e29b-41d4-a716-446655440000', summary: 'first' },
            { node_id: '550e8400-e29b-41d4-a716-446655440000', summary: 'second' },
          ],
        },
      },
    })
    await wrapper.find('[data-testid="hitl-briefing-toggle"]').trigger('click')
    const summaries = wrapper.findAll('[data-testid="hitl-briefing-details"] pre')
    expect(summaries).toHaveLength(2)
    expect(summaries[0].text()).toBe('first')
    expect(summaries[1].text()).toBe('second')
  })

  it('renders the unknown trigger for an unresolvable gate config (FAR-688)', async () => {
    const wrapper = mount(HitlBriefing, {
      props: { description: 'Why this gate exists.', context: { ...fullContext, trigger: 'unknown' } },
    })
    await wrapper.find('[data-testid="hitl-briefing-toggle"]').trigger('click')
    expect(wrapper.find('[data-testid="hitl-briefing-details"]').text()).toContain(
      'Trigger unknown (gate config could not be resolved)',
    )
  })

  it('renders bounded artifact excerpts in the details', async () => {
    const wrapper = mount(HitlBriefing, {
      props: { description: 'Why this gate exists.', context: fullContext },
    })
    await wrapper.find('[data-testid="hitl-briefing-toggle"]').trigger('click')
    const details = wrapper.find('[data-testid="hitl-briefing-details"]')
    expect(details.text()).toContain('{"comment":"Looks good"}')
  })

  it('shows the empty-artifacts note when context has no artifact entries', async () => {
    const wrapper = mount(HitlBriefing, {
      props: { description: 'Why this gate exists.', context: { ...fullContext, artifacts: [] } },
    })
    await wrapper.find('[data-testid="hitl-briefing-toggle"]').trigger('click')
    expect(wrapper.find('[data-testid="hitl-briefing-details"]').text()).toContain(
      'No matching node output was captured for this gate.',
    )
  })

  it('hides the details toggle when there is no fire-time context', () => {
    const wrapper = mount(HitlBriefing, { props: { description: null, context: null } })
    expect(wrapper.find('[data-testid="hitl-briefing-toggle"]').exists()).toBe(false)
  })
})
