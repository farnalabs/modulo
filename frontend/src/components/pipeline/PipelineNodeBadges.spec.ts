import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PipelineNodeBadges from './PipelineNodeBadges.vue'

// FAR-2462: every badge is DERIVED from a field on the pipeline graph
// contract. These cases pin the derivation rules and — just as importantly —
// the negative cases, so the component can never announce a binding that the
// graph does not actually carry.
function mountBadges(node: Record<string, unknown>) {
  return mount(PipelineNodeBadges, { props: { node } })
}

function testIds(wrapper: ReturnType<typeof mountBadges>): string[] {
  return wrapper.findAll('[data-testid^="pipeline-node-semantic-"]')
    .map(el => el.attributes('data-testid'))
    .filter((id): id is string => typeof id === 'string')
    .filter(id => id !== 'pipeline-node-semantic-badges')
}

describe('PipelineNodeBadges', () => {
  it('renders nothing when the node carries no semantic contract field', () => {
    const wrapper = mountBadges({ node_type: 'agent', label: 'Agent' })

    expect(wrapper.find('[data-testid="pipeline-node-semantic-badges"]').exists()).toBe(false)
    expect(testIds(wrapper)).toEqual([])
    wrapper.unmount()
  })

  it('does not claim a connector when the binding has no instance id', () => {
    const wrapper = mountBadges({
      node_type: 'agent',
      connector_binding: { type: 'slack', instance_id: '', operation: 'query', dispatch_action: 'trigger_run' },
    })

    expect(testIds(wrapper)).toEqual([])
    wrapper.unmount()
  })

  it('states the connector type, allowed operations, and bound operation verb', () => {
    const wrapper = mount(PipelineNodeBadges, {
      props: {
        node: {
          node_type: 'agent',
          connector_binding: { type: 'github', instance_id: 'conn-1', operation: 'write', dispatch_action: 'trigger_run' },
        },
        connectors: [{ id: 'conn-1', allowed_operations: ['query', 'write'] }],
      },
    })

    const ids = testIds(wrapper)
    expect(ids).toContain('pipeline-node-semantic-connector')
    expect(ids).toContain('pipeline-node-semantic-allowed-operations')
    expect(ids).toContain('pipeline-node-semantic-operation')

    const connector = wrapper.find('[data-testid="pipeline-node-semantic-connector"]')
    expect(connector.text()).toContain('github')
    expect(wrapper.find('[data-testid="pipeline-node-semantic-allowed-operations"]').text()).toContain('query, write')
    expect(wrapper.find('[data-testid="pipeline-node-semantic-operation"]').text()).toContain('write')
    wrapper.unmount()
  })

  it('omits the operation badge for a dispatch binding, which the dispatch badge already names', () => {
    const wrapper = mountBadges({
      node_type: 'dispatch',
      connector_binding: {
        type: 'ci',
        instance_id: 'conn-2',
        operation: 'dispatch',
        dispatch_action: 'trigger_run',
        resource: 'build-app',
      },
    })

    const ids = testIds(wrapper)
    expect(ids).not.toContain('pipeline-node-semantic-operation')

    const dispatch = wrapper.find('[data-testid="pipeline-node-semantic-dispatch"]')
    expect(dispatch.text()).toContain('build-app')
    expect(dispatch.text()).toContain('trigger_run')
    wrapper.unmount()
  })

  it('renders detail-free connector, dispatch and HITL badges without inventing a detail', () => {
    // A binding with no `type` still names a real connector, but there is no
    // detail to show, so the title falls back to the bare label.
    const connector = mountBadges({
      node_type: 'agent',
      connector_binding: { instance_id: 'conn-3', operation: 'dispatch' },
    })
    const connectorBadge = connector.find('[data-testid="pipeline-node-semantic-connector"]')
    expect(connectorBadge.exists()).toBe(true)
    expect(connectorBadge.attributes('title')).toBe('connector')
    // `operation: dispatch` is left to the dispatch badge, which this agent
    // node cannot render, so no operation badge is claimed.
    expect(testIds(connector)).not.toContain('pipeline-node-semantic-operation')
    connector.unmount()

    // A dispatch binding that names no resource or action has nothing to say
    // beyond the connector badge.
    const dispatch = mountBadges({
      node_type: 'dispatch',
      connector_binding: { instance_id: 'conn-4', operation: 'dispatch' },
    })
    expect(testIds(dispatch)).not.toContain('pipeline-node-semantic-dispatch')
    dispatch.unmount()

    // A HITL gate with no label renders the gate badge, still without detail.
    const gate = mountBadges({ node_type: 'hitl', hitl_config: {} })
    const hitlBadge = gate.find('[data-testid="pipeline-node-semantic-hitl"]')
    expect(hitlBadge.exists()).toBe(true)
    expect(hitlBadge.attributes('title')).toBe('hitl gate')
    gate.unmount()
  })

  it('counts real router rules and stays silent for an empty rule set', () => {
    const withRules = mountBadges({
      node_type: 'router',
      router_config: { mode: 'rules', rules: [{ guard: 'x', target: 'node-b' }, { default: true, target: 'node-c' }] },
    })
    expect(withRules.find('[data-testid="pipeline-node-semantic-router"]').text()).toContain('2')
    withRules.unmount()

    const withoutRules = mountBadges({ node_type: 'router', router_config: { mode: 'rules', rules: [] } })
    expect(withoutRules.find('[data-testid="pipeline-node-semantic-router"]').exists()).toBe(false)
    withoutRules.unmount()
  })

  it('reports a HITL gate and its eval-referenced condition only where the config declares one', () => {
    const gate = mountBadges({
      node_type: 'hitl',
      hitl_config: { label: 'Release gate', eval_condition: { eval_name: 'quality', threshold: 0.8, operator: 'gte' } },
    })

    const ids = testIds(gate)
    expect(ids).toContain('pipeline-node-semantic-hitl')
    expect(ids).toContain('pipeline-node-semantic-eval')
    expect(gate.find('[data-testid="pipeline-node-semantic-hitl"]').text()).toContain('Release gate')
    expect(gate.find('[data-testid="pipeline-node-semantic-eval"]').text()).toContain('quality')
    gate.unmount()

    const plainGate = mountBadges({ node_type: 'hitl', hitl_config: { label: 'Plain gate' } })
    expect(testIds(plainGate)).not.toContain('pipeline-node-semantic-eval')
    plainGate.unmount()
  })
})
