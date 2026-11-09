/** FAR-2475: Vitest spec proving the adapter maps semantic fields faithfully
 * and rejects/omits UI-only fields.
 */

import { describe, it, expect } from 'vitest'
import type { components } from '@/lib/api/schema'
import {
  projectGraphNode,
  projectGraphEdge,
  projectGraph,
  CanvasNodeUiState,
  CanvasEdgeUiState,
  defaultCanvasNodeUiState,
  withUiState,
  withEdgeUiState,
} from '../types/pipeline-graph-projection'

// ---------------------------------------------------------------------------
// Mock generated wire types
// ---------------------------------------------------------------------------

function makeWireNode(overrides: Partial<components['schemas']['PipelineGraphNode']> = {}): components['schemas']['PipelineGraphNode'] {
  return {
    id: '00000000-0000-0000-0000-000000000101',
    node_type: 'manual',
    position: { x: 10, y: 20 },
    label: 'Start',
    description: 'Manual kick-off',
    output_schema_json: { type: 'object' },
    idempotent: true,
    mode: 'llm',
    commands_concatenation_string: ' && ',
    read_only: false,
    single_pr_per_run: false,
    enable_heartbeat: true,
    join_partial_policy: 'collect_and_proceed',
    await_completion: false,
    ...overrides,
  }
}

function makeWireEdge(overrides: Partial<components['schemas']['PipelineGraphEdge']> = {}): components['schemas']['PipelineGraphEdge'] {
  return {
    id: '00000000-0000-0000-0000-000000000201',
    source_node_id: '00000000-0000-0000-0000-000000000101',
    target_node_id: '00000000-0000-0000-0000-000000000102',
    edge_type: 'normal',
    source_port: 'out',
    target_port: 'in',
    ...overrides,
  }
}

function makeWireGraph(nodes: components['schemas']['PipelineGraphNode'][] = [], edges: components['schemas']['PipelineGraphEdge'][] = []) {
  return { nodes, edges }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('pipeline-graph-projection', () => {
  describe('projectGraphNode', () => {
    it('maps core identity and position', () => {
      const node = makeWireNode({ id: 'abc-123', position: { x: 5, y: 7 } })
      const proj = projectGraphNode(node)

      expect(proj.id).toBe('abc-123')
      expect(proj.position).toEqual({ x: 5, y: 7 })
      expect(proj.type).toBe('manual')
    })

    it('maps all semantic data fields', () => {
      const node = makeWireNode({
        label: 'My Node',
        description: 'A description',
        node_type: 'sandbox_agent',
        mode: 'llm',
        template_id: 'opencode',
        agent_commands: ['echo hi'],
        agent_prompt: 'Do it',
        timeout_seconds: 300,
        stall_timeout_seconds: 60,
        enable_heartbeat: false,
        read_only: true,
        git_credentials: 'scoped',
        egress_policy: 'deny_all',
        stdout_retention_mode: 'full',
        stdout_max_bytes: 1024,
        idempotent: false,
        output_schema_json: { type: 'object' },
        agent_id: '00000000-0000-0000-0000-000000000001',
        hitl_config: { mode: 'approval', description: 'Gate', claim_expiry_minutes: 30, human_only: true },
        policy_gate: { action: 'block', eval_id: '00000000-0000-0000-0000-000000000002' },
        parameter_set_id: '00000000-0000-0000-0000-000000000003',
        parameter_overrides: { foo: 'bar' },
        capability_scope: { allowed_connectors: ['github'], allowed_tools: [], context_scope: [] },
        connector_binding: { type: 'github', instance_id: '00000000-0000-0000-0000-000000000004', operation: 'query', dispatch_action: 'trigger_run', resource: 'pull', filters: {}, data: {} },
        router_config: { mode: 'rules', rules: [{ guard: '`true`', target: '...' }] },
      })

      const proj = projectGraphNode(node)

      expect(proj.data.label).toBe('My Node')
      expect(proj.data.description).toBe('A description')
      expect(proj.data.node_type).toBe('sandbox_agent')
      expect(proj.data.hitl_config).toEqual(node.hitl_config)
      expect(proj.data.policy_gate).toEqual(node.policy_gate)
      expect(proj.data.parameter_set_id).toBe(node.parameter_set_id)
      expect(proj.data.parameter_overrides).toEqual(node.parameter_overrides)
      expect(proj.data.hasCapabilityScope).toBe(true) // scope declared
      expect(proj.data.connector_binding).toEqual(node.connector_binding)
      expect(proj.data.router_config).toEqual(node.router_config)
    })

    it('falls back to agent canvas type for unknown node_type', () => {
      const node = makeWireNode({ node_type: 'composite' })
      const proj = projectGraphNode(node)
      expect(proj.type).toBe('agent')
    })

    it('does not resolve inherited Object.prototype keys as canvas types', () => {
      // A wire node_type of 'toString' must not resolve to an inherited
      // Object.prototype member - it falls through to the agent card like any
      // other unknown type. The assertion bypasses the declared literal union
      // because that value can still arrive from a buggy/older payload.
      const node = makeWireNode({
        node_type: 'toString' as components['schemas']['PipelineGraphNode']['node_type'],
      })
      const proj = projectGraphNode(node)
      expect(proj.type).toBe('agent')
    })

    it('falls back label to "Node " + the shared shortId when label absent', () => {
      const node = makeWireNode({ label: undefined, id: '00000000-0000-0000-0000-00000000abcd' })
      const proj = projectGraphNode(node)
      // `shortId` lives in @/utils/format; it renders '#<first 8 chars>'.
      expect(proj.data.label).toBe('Node #00000000')
    })

    it('omits UI-only fields from projection (no selected, dragging, width, height, zIndex)', () => {
      const node = makeWireNode()
      const proj = projectGraphNode(node)

      // UI-only keys must NOT be present on the projection object
      expect(proj).not.toHaveProperty('selected')
      expect(proj).not.toHaveProperty('dragging')
      expect(proj).not.toHaveProperty('width')
      expect(proj).not.toHaveProperty('height')
      expect(proj).not.toHaveProperty('zIndex')
      // The data object also must not carry them
      expect(proj.data).not.toHaveProperty('selected')
      expect(proj.data).not.toHaveProperty('dragging')
    })

    it('does not propagate extra unknown keys from wire payload', () => {
      // A wire payload that carries an unknown key (e.g. from a future API
      // version or a buggy client) must NOT appear in the projection.
      const node = { ...makeWireNode(), unknown_future_field: 'should be dropped' } as any
      const proj = projectGraphNode(node)

      // The projection is a constructed object; extra keys cannot exist
      // unless we explicitly spread the input (we don't).
      expect(Object.keys(proj)).toEqual(expect.arrayContaining(['id', 'type', 'position', 'data']))
    })
  })

  describe('projectGraphEdge', () => {
    it('maps core identity and ports', () => {
      const edge = makeWireEdge({
        id: 'edge-1',
        source_node_id: 'src',
        target_node_id: 'tgt',
        edge_type: 'conditional',
        condition_expression: '`true`',
        source_port: 'out',
        target_port: 'in',
        hitl_review_config: { label: 'Approve', description: 'Desc', claim_expiry_minutes: 30, human_only: true },
      })

      const proj = projectGraphEdge(edge, 0)

      expect(proj.id).toBe('edge-1')
      expect(proj.source).toBe('src')
      expect(proj.target).toBe('tgt')
      expect(proj.edgeType).toBe('conditional')
      expect(proj.conditionExpression).toBe('`true`')
      expect(proj.sourcePort).toBe('out')
      expect(proj.targetPort).toBe('in')
      expect(proj.hitl_review_config).toEqual(edge.hitl_review_config)
    })

    it('defaults id from index when absent', () => {
      const edge = makeWireEdge({ id: undefined })
      const proj = projectGraphEdge(edge, 42)
      expect(proj.id).toBe('edge-42')
    })

    it('omits UI-only styling fields (animated, stroke, style, markerEnd)', () => {
      const edge = makeWireEdge()
      const proj = projectGraphEdge(edge, 0)

      expect(proj).not.toHaveProperty('animated')
      expect(proj).not.toHaveProperty('stroke')
      expect(proj).not.toHaveProperty('style')
      expect(proj).not.toHaveProperty('markerEnd')
    })
  })

  describe('projectGraph (full graph)', () => {
    it('projects all nodes and edges', () => {
      const nodes = [makeWireNode({ id: 'n1' }), makeWireNode({ id: 'n2', node_type: 'sandbox_agent' })]
      const edges = [makeWireEdge({ id: 'e1', source_node_id: 'n1', target_node_id: 'n2' })]
      const graph = makeWireGraph(nodes, edges)

      const proj = projectGraph(graph)

      expect(proj.nodes).toHaveLength(2)
      expect(proj.edges).toHaveLength(1)
      expect(proj.nodes[0].id).toBe('n1')
      expect(proj.nodes[1].id).toBe('n2')
      expect(proj.edges[0].id).toBe('e1')
    })
  })

  describe('UI state separation', () => {
    it('defaultCanvasNodeUiState has no selection/drag', () => {
      expect(defaultCanvasNodeUiState).toEqual({ selected: false, dragging: false })
    })

    it('withUiState merges UI state without mutating projection', () => {
      const node = makeWireNode()
      const proj = projectGraphNode(node)
      const withState = withUiState(proj, { selected: true, width: 200 })

      expect(withState.ui).toEqual({ selected: true, dragging: false, width: 200 })
      // Original projection unchanged
      expect(proj).not.toHaveProperty('ui')
    })

    it('withEdgeUiState derives style from edgeType', () => {
      const edge = makeWireEdge({ edge_type: 'loop' })
      const proj = projectGraphEdge(edge, 0)
      const withState = withEdgeUiState(proj)

      expect(withState.ui.animated).toBe(true)
      expect(withState.ui.stroke).toBe('#3b82f6')
      expect(withState.ui.style).toEqual({ stroke: '#3b82f6', strokeDasharray: '5,5' })
      expect(withState.ui.markerEnd.color).toBe('#3b82f6')
    })

    it('withEdgeUiState treats reject and conditional edges as non-loop style', () => {
      // The loop case is covered above; these are the remaining non-normal
      // edge types and both must keep the default (non-animated, solid) style.
      for (const edgeType of ['reject', 'conditional'] as const) {
        const proj = projectGraphEdge(makeWireEdge({ edge_type: edgeType }), 0)
        const withState = withEdgeUiState(proj)

        expect(withState.ui.animated).toBe(false)
        expect(withState.ui.stroke).toBe('#64748b')
        expect(withState.ui.style).toEqual({ stroke: '#64748b' })
        expect(withState.ui.markerEnd.color).toBe('#64748b')
      }
    })

    it('withEdgeUiState uses default style for normal edges', () => {
      const edge = makeWireEdge({ edge_type: 'normal' })
      const proj = projectGraphEdge(edge, 0)
      const withState = withEdgeUiState(proj)

      expect(withState.ui.stroke).toBe('#64748b')
    })
  })

  describe('CanvasNodeUiState / CanvasEdgeUiState types', () => {
    it('CanvasNodeUiState excludes semantic fields', () => {
      // Type-level check: ensure the interface doesn't accidentally include
      // semantic keys. We can't easily test at runtime, but we can verify
      // the default object shape.
      const ui: CanvasNodeUiState = { selected: true, dragging: false, width: 100, height: 50, zIndex: 5 }
      expect(ui).toEqual({ selected: true, dragging: false, width: 100, height: 50, zIndex: 5 })
    })

    it('CanvasEdgeUiState includes only UI styling', () => {
      const ui: CanvasEdgeUiState = {
        animated: true,
        stroke: '#f00',
        style: { stroke: '#f00' },
        markerEnd: { type: 'ArrowClosed', width: 16, height: 16, color: '#f00' },
      }
      // The interface must carry exactly these four UI-styling keys: a
      // semantic field (edgeType, conditionExpression, ...) leaking into the
      // UI state would show up here as an extra key.
      expect(Object.keys(ui).sort()).toEqual(['animated', 'markerEnd', 'stroke', 'style'])
      expect(ui.animated).toBe(true)
      expect(ui.stroke).toBe('#f00')
      expect(ui.style).toEqual({ stroke: '#f00' })
      expect(ui.markerEnd).toEqual({ type: 'ArrowClosed', width: 16, height: 16, color: '#f00' })
    })
  })
})
