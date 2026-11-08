import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import {
  NODE_REGISTRY,
  NODE_REGISTRY_ORDER,
  getNodeRegistryEntry,
  getNodeTheme,
  getNodeType,
  nodeHasConfigPanel,
} from '../components/pipeline/nodes/NodeRegistry'
import AgentNode from '../components/pipeline/nodes/AgentNode.vue'
import ManualNode from '../components/pipeline/nodes/ManualNode.vue'
import PipelineNodeBase from '../components/pipeline/nodes/PipelineNodeBase.vue'

describe('pipeline node registry', () => {
  it('contains every supported node type with the required metadata', () => {
    expect(NODE_REGISTRY_ORDER).toHaveLength(6)
    for (const nodeType of NODE_REGISTRY_ORDER) {
      const entry = NODE_REGISTRY[nodeType]
      expect(entry.id).toBe(nodeType)
      expect(entry.labelKey).toBeTruthy()
      expect(entry.glyphKey).toBeTruthy()
      expect(entry.fallbackLabelKey).toBeTruthy()
      expect(entry.theme.border).toBeTruthy()
      expect(entry.theme.labelColour).toBeTruthy()
      expect(typeof entry.hasConfigPanel).toBe('boolean')
    }
  })

  it('falls back to the agent entry for unknown types', () => {
    expect(getNodeRegistryEntry('not-real')).toBe(NODE_REGISTRY.agent)
  })

  it('exposes theme and config-panel helpers, falling back for unknown types', () => {
    expect(getNodeTheme('router')).toBe(NODE_REGISTRY.router.theme)
    expect(getNodeTheme('not-real')).toBe(NODE_REGISTRY.agent.theme)
    expect(nodeHasConfigPanel('hitl')).toBe(true)
    expect(nodeHasConfigPanel('dispatch')).toBe(false)
    expect(nodeHasConfigPanel('not-real')).toBe(true)
  })

  it('defaults a data object without node_type to the agent type', () => {
    expect(getNodeType({})).toBe('agent')
    expect(getNodeType({ node_type: 'connector' })).toBe('connector')
  })
})

describe('pipeline node components', () => {
  it('renders the manual card with its fallback label', () => {
    const wrapper = mount(ManualNode, { props: { id: 'manual-1', data: { node_type: 'manual' } } })
    expect(wrapper.text()).toContain('Manual')
    expect(wrapper.find('[data-testid="pipeline-node-manual"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders the agent scope badge when capability scope is set', () => {
    const wrapper = mount(AgentNode, {
      props: {
        id: 'agent-1',
        selected: true,
        data: { label: 'Writer', description: 'Draft copy', hasCapabilityScope: true },
      },
    })
    expect(wrapper.text()).toContain('Writer')
    expect(wrapper.text()).toContain('Draft copy')
    expect(wrapper.find('[data-testid="pipeline-node-scope-badge"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders the agent fallback label without a scope badge', () => {
    const wrapper = mount(AgentNode, { props: { id: 'agent-2', data: {} } })
    expect(wrapper.text()).toContain('Agent')
    expect(wrapper.find('[data-testid="pipeline-node-scope-badge"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('honours an explicit test id and selected styling on the base card', () => {
    const wrapper = mount(PipelineNodeBase, {
      props: {
        id: 'base-1',
        selected: true,
        testId: 'custom-node',
        data: { node_type: 'agent', label: 'Base', description: 'Shown' },
      },
    })
    expect(wrapper.attributes('data-testid')).toBe('custom-node')
    expect(wrapper.attributes('class')).toContain('ring-2')
    wrapper.unmount()
  })
})
