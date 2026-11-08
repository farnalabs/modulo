import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import EdgeBadges from '../components/pipeline/nodes/EdgeBadges.vue'

describe('EdgeBadges', () => {
  it('renders HITL, loop and LLM badges from edge metadata', () => {
    const wrapper = mount(EdgeBadges, {
      props: {
        data: {
          hitl_review_config: { description: 'Review' },
          edge_type: 'loop',
          max_iterations: 3,
          routing_label: '',
        },
      },
    })

    expect(wrapper.find('[data-testid="edge-badge-hitl"]').text()).toBe('HITL')
    expect(wrapper.find('[data-testid="edge-badge-loop"]').text()).toBe('Loop (3)')
    expect(wrapper.find('[data-testid="edge-badge-llm"]').exists()).toBe(false)
  })

  it('renders the LLM routing label and omits unrelated badges', () => {
    const wrapper = mount(EdgeBadges, {
      props: { data: { edge_type: 'llm', routing_label: 'fast-model' } },
    })

    expect(wrapper.find('[data-testid="edge-badge-llm"]').text()).toBe('LLM: fast-model')
    expect(wrapper.find('[data-testid="edge-badge-hitl"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="edge-badge-loop"]').exists()).toBe(false)
  })

  it('renders no badges for normal edges', () => {
    const wrapper = mount(EdgeBadges, { props: { data: { edge_type: 'normal' } } })
    expect(wrapper.find('[data-testid^="edge-badge-"]').exists()).toBe(false)
  })

  it('renders loop and LLM badges without optional labels', () => {
    const loop = mount(EdgeBadges, { props: { data: { edge_type: 'loop' } } })
    expect(loop.find('[data-testid="edge-badge-loop"]').text()).toBe('Loop')
    loop.unmount()

    const llm = mount(EdgeBadges, { props: { data: { edge_type: 'llm' } } })
    expect(llm.find('[data-testid="edge-badge-llm"]').text()).toBe('LLM')
    llm.unmount()
  })

  it('treats a missing data payload as an empty edge', () => {
    const wrapper = mount(EdgeBadges, { props: { data: null } })
    expect(wrapper.find('[data-testid^="edge-badge-"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
