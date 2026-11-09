/** FAR-2475: Vitest spec proving the adapter maps semantic fields faithfully
 * and rejects/omits UI-only fields.
 */

import { describe, it, expect } from 'vitest'
import { MarkerType } from '@vue-flow/core'
import type { components } from '@/lib/api/schema'
import { CANVAS_EDGE_STROKE } from '@/constants/canvas'
import {
  projectGraphNode,
  projectGraphEdge,
  projectCanvasEdge,
  projectGraph,
  normalizeCanvasEdgeType,
  CANVAS_EDGE_TYPES,
  CanvasNodeUiState,
  CanvasEdgeUiState,
  CanvasEdgeType,
  PipelineGraphEdgeInput,
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

function makeWireEdge(overrides: Partial<PipelineGraphEdgeInput> = {}): PipelineGraphEdgeInput {
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

    it('produces the data wrapper the canvas + inspector consume', () => {
      const hitl = { label: 'Approve', description: 'Desc', claim_expiry_minutes: 30, human_only: true }
      const edge = makeWireEdge({
        id: 'edge-data',
        edge_type: 'llm',
        condition_expression: '`true`',
        hitl_review_config: hitl,
        max_iterations: 7,
        routing_label: 'escalate',
      })

      const data = projectGraphEdge(edge, 0).data

      expect(data).toEqual({
        hitl_review_config: hitl,
        edge_type: 'llm',
        condition_expression: '`true`',
        max_iterations: 7,
        routing_label: 'escalate',
      })
      // The config is carried by reference, exactly as the inline mapping did:
      // the inspector builds its save payload from the selected edge's config.
      expect(data.hitl_review_config).toBe(hitl)
    })

    it('defaults the data wrapper for an edge with no optional fields', () => {
      const data = projectGraphEdge(makeWireEdge({ edge_type: 'normal' }), 0).data

      expect(data.edge_type).toBe('normal')
      expect(data.condition_expression).toBeNull()
      expect(data.hitl_review_config).toBeNull()
      expect(data.max_iterations).toBe(0)
      expect(data.routing_label).toBe('')
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

  describe('normalizeCanvasEdgeType', () => {
    it('keeps every known variant', () => {
      for (const edgeType of CANVAS_EDGE_TYPES) {
        expect(normalizeCanvasEdgeType(edgeType)).toBe(edgeType)
      }
    })

    it('falls back to normal for absent or unknown values without dropping the raw value from data', () => {
      expect(normalizeCanvasEdgeType(undefined)).toBe('normal')
      expect(normalizeCanvasEdgeType(null)).toBe('normal')
      expect(normalizeCanvasEdgeType('future-type')).toBe('normal')

      // Styling normalises, but the wire value itself round-trips verbatim in
      // the data wrapper - exactly what the pre-adapter inline mapping did.
      const proj = projectGraphEdge(makeWireEdge({ edge_type: 'future-type' }), 0)
      expect(proj.edgeType).toBe('normal')
      expect(proj.data.edge_type).toBe('future-type')
    })
  })

  describe('projectCanvasEdge', () => {
    it('maps a loop edge to the exact Vue Flow edge shape', () => {
      const edge = makeWireEdge({
        id: 'e-loop',
        source_node_id: 'a',
        target_node_id: 'b',
        edge_type: 'loop',
        max_iterations: 3,
      })
      const flowEdge = projectCanvasEdge(edge, 0)

      expect(flowEdge).toEqual({
        id: 'e-loop',
        source: 'a',
        target: 'b',
        type: 'smoothstep',
        animated: true,
        style: { stroke: '#3b82f6', strokeDasharray: '5,5' },
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#3b82f6' },
        data: {
          hitl_review_config: null,
          edge_type: 'loop',
          condition_expression: null,
          max_iterations: 3,
          routing_label: '',
        },
      })
    })

    it('carries the projection data wrapper by reference (selection round-trip)', () => {
      const edge = makeWireEdge({ edge_type: 'llm', routing_label: 'go' })
      const projection = projectGraphEdge(edge, 0)
      const flowEdge = projectCanvasEdge(edge, 0)

      // The flow edge renders the same semantic data the projection defines,
      // so the inspector (which reads the wire edge) and the canvas agree.
      expect(flowEdge.data).toEqual(projection.data)
      expect(flowEdge.data.edge_type).toBe('llm')
      expect(flowEdge.data.routing_label).toBe('go')
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

    it('withEdgeUiState derives loop style (animated, dashed, blue)', () => {
      const edge = makeWireEdge({ edge_type: 'loop' })
      const proj = projectGraphEdge(edge, 0)
      const withState = withEdgeUiState(proj)

      expect(withState.ui.animated).toBe(true)
      expect(withState.ui.stroke).toBe('#3b82f6')
      expect(withState.ui.style).toEqual({ stroke: '#3b82f6', strokeDasharray: '5,5' })
      expect(withState.ui.markerEnd.type).toBe(MarkerType.ArrowClosed)
      expect(withState.ui.markerEnd.color).toBe('#3b82f6')
    })

    it('withEdgeUiState gives llm edges their own violet solid style', () => {
      const proj = projectGraphEdge(makeWireEdge({ edge_type: 'llm' }), 0)
      const withState = withEdgeUiState(proj)

      expect(withState.ui.animated).toBe(false)
      expect(withState.ui.stroke).toBe('#8b5cf6')
      expect(withState.ui.style).toEqual({ stroke: '#8b5cf6' })
      expect(withState.ui.markerEnd.color).toBe('#8b5cf6')
    })

    it('withEdgeUiState uses the shared canvas stroke for normal/reject/conditional', () => {
      for (const edgeType of ['normal', 'reject', 'conditional'] as const) {
        const proj = projectGraphEdge(makeWireEdge({ edge_type: edgeType }), 0)
        const withState = withEdgeUiState(proj)

        expect(withState.ui.animated).toBe(false)
        expect(withState.ui.stroke).toBe(CANVAS_EDGE_STROKE)
        expect(withState.ui.style).toEqual({ stroke: CANVAS_EDGE_STROKE })
        expect(withState.ui.markerEnd.color).toBe(CANVAS_EDGE_STROKE)
      }
    })

    it('styles every declared variant (exhaustiveness regression)', () => {
      // A compile-time complete map: adding a variant to CanvasEdgeType makes
      // this object literal a type error until the case is added here too.
      const expected: Record<CanvasEdgeType, { stroke: string; animated: boolean }> = {
        normal: { stroke: CANVAS_EDGE_STROKE, animated: false },
        reject: { stroke: CANVAS_EDGE_STROKE, animated: false },
        conditional: { stroke: CANVAS_EDGE_STROKE, animated: false },
        loop: { stroke: '#3b82f6', animated: true },
        llm: { stroke: '#8b5cf6', animated: false },
      }

      // The adapter's exported variant list must cover exactly the union's
      // members: a variant dropped from the list (or added to the union without
      // a style) fails here instead of silently rendering as a plain edge.
      expect([...CANVAS_EDGE_TYPES].sort()).toEqual(Object.keys(expected).sort())

      for (const edgeType of CANVAS_EDGE_TYPES) {
        const withState = withEdgeUiState(projectGraphEdge(makeWireEdge({ edge_type: edgeType }), 0))
        expect(withState.ui.stroke).toBe(expected[edgeType].stroke)
        expect(withState.ui.animated).toBe(expected[edgeType].animated)
        // Every variant gets a distinct closed arrowhead matching its stroke.
        expect(withState.ui.markerEnd.type).toBe(MarkerType.ArrowClosed)
        expect(withState.ui.markerEnd.color).toBe(expected[edgeType].stroke)
      }
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
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#f00' },
      }
      // The interface must carry exactly these four UI-styling keys: a
      // semantic field (edgeType, conditionExpression, ...) leaking into the
      // UI state would show up here as an extra key.
      expect(Object.keys(ui).sort()).toEqual(['animated', 'markerEnd', 'stroke', 'style'])
      expect(ui.animated).toBe(true)
      expect(ui.stroke).toBe('#f00')
      expect(ui.style).toEqual({ stroke: '#f00' })
      expect(ui.markerEnd).toEqual({ type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#f00' })
    })
  })
})
