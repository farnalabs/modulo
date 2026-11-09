import { describe, expect, it } from 'vitest'
import { createSystemMapRendererModel } from '../utils/system-map-renderer'
import type { SystemMapNode, SystemMapRelationship } from '../stores/systemMaps'

function node(overrides: Partial<SystemMapNode> = {}): SystemMapNode {
  return {
    id: 'node-id', organisation_id: '', revision_id: 'revision', node_key: 'pipeline-1', node_type: 'pipeline', display_name: 'Pipeline', config_json: {}, pipeline_id: null, connector_instance_id: null, owner_team_id: null, eval_id: null, position_x: '12', position_y: '24', deleted_at: null, deleted_by: null, created_at: '', updated_at: '', ...overrides,
  }
}

function relationship(overrides: Partial<SystemMapRelationship> = {}): SystemMapRelationship {
  return {
    id: 'relationship-id', organisation_id: '', revision_id: 'revision', source_node_key: 'pipeline-1', target_node_key: 'agent-1', relationship_type: 'flow', config_json: {}, deleted_at: null, deleted_by: null, created_at: '', updated_at: '', ...overrides,
  }
}

describe('createSystemMapRendererModel', () => {
  it('maps stable identity, positions, selection, read-only state, and edge endpoints/type', () => {
    const model = createSystemMapRendererModel(
      [node(), node({ id: 'agent-id', node_key: 'agent-1', node_type: 'agent', display_name: 'Agent', position_x: '80', position_y: '96' })],
      [relationship()],
      { selectedNodeKey: 'agent-1', readOnly: true },
    )

    expect(model.nodes).toEqual([
      { id: 'node-id', nodeKey: 'pipeline-1', type: 'pipeline', label: 'Pipeline', x: 12, y: 24, selected: false, readOnly: true },
      { id: 'agent-id', nodeKey: 'agent-1', type: 'agent', label: 'Agent', x: 80, y: 96, selected: true, readOnly: true },
    ])
    expect(model.edges).toEqual([{ id: 'relationship-id', source: 'pipeline-1', target: 'agent-1', type: 'flow' }])
    expect(model.readOnly).toBe(true)
  })

  it('omits relationships whose endpoints are not renderable nodes', () => {
    const model = createSystemMapRendererModel([node()], [relationship()])
    expect(model.edges).toEqual([])
  })

  it('preserves an empty graph as an empty renderer model', () => {
    expect(createSystemMapRendererModel([], [])).toEqual({ nodes: [], edges: [], readOnly: false })
  })
})
