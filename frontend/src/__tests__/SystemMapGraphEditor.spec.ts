import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'
import SystemMapGraphEditor from '../components/system-maps/SystemMapGraphEditor.vue'
import { NODE_TYPES, RELATIONSHIP_TYPES, type SystemMapNode } from '../stores/systemMaps'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

function node(nodeKey: string): SystemMapNode {
  return {
    id: nodeKey,
    organisation_id: '',
    revision_id: '',
    node_key: nodeKey,
    node_type: 'pipeline',
    display_name: nodeKey,
    config_json: {},
    pipeline_id: null,
    connector_instance_id: null,
    owner_team_id: null,
    eval_id: null,
    position_x: '0',
    position_y: '0',
    deleted_at: null,
    deleted_by: null,
    created_at: '1970-01-01T00:00:00.000Z',
    updated_at: '1970-01-01T00:00:00.000Z',
  }
}

describe('SystemMapGraphEditor', () => {
  it('keeps node keys unique after removing and re-adding a node type', async () => {
    const wrapper = mount(SystemMapGraphEditor, {
      props: { nodes: [], relationships: [], loopConfigs: [], nodeTypes: NODE_TYPES, relationshipTypes: RELATIONSHIP_TYPES },
      global: { plugins: [i18n] },
    })

    await wrapper.get('[data-testid="system-map-add-node-pipeline"]').trigger('click')
    await wrapper.get('[data-testid="system-map-remove-node-pipeline-1"]').trigger('click')
    await wrapper.get('[data-testid="system-map-add-node-pipeline"]').trigger('click')

    expect(wrapper.find('[data-testid="system-map-node-pipeline-2"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-node-pipeline-1"]').exists()).toBe(false)
  })

  it('seeds counters from existing numeric node keys', async () => {
    const wrapper = mount(SystemMapGraphEditor, {
      props: { nodes: [node('pipeline-4')], relationships: [], loopConfigs: [], nodeTypes: NODE_TYPES, relationshipTypes: RELATIONSHIP_TYPES },
      global: { plugins: [i18n] },
    })

    await wrapper.get('[data-testid="system-map-add-node-pipeline"]').trigger('click')

    expect(wrapper.find('[data-testid="system-map-node-pipeline-5"]').exists()).toBe(true)
  })

  it('marks the clicked node as selected and clears the previous selection', async () => {
    const wrapper = mount(SystemMapGraphEditor, {
      props: { nodes: [node('pipeline-1'), node('pipeline-2')], relationships: [], loopConfigs: [], nodeTypes: NODE_TYPES, relationshipTypes: RELATIONSHIP_TYPES },
      global: { plugins: [i18n] },
    })

    const first = wrapper.get('[data-testid="system-map-node-pipeline-1"]')
    const second = wrapper.get('[data-testid="system-map-node-pipeline-2"]')
    expect(first.attributes('data-selected')).toBe('false')
    expect(second.attributes('data-selected')).toBe('false')

    await first.trigger('click')
    expect(first.attributes('data-selected')).toBe('true')
    expect(second.attributes('data-selected')).toBe('false')

    await second.trigger('click')
    expect(first.attributes('data-selected')).toBe('false')
    expect(second.attributes('data-selected')).toBe('true')
  })
})
