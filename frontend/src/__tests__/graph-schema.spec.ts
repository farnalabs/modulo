import { describe, expect, it } from 'vitest'
import {
  CanonicalPipelineGraphEdgeSchema,
  CanonicalPipelineGraphNodeSchema,
  safeValidateCanonicalGraph,
  validateCanonicalGraph,
} from '../types/pipeline-graph'

const NODE_ID = '00000000-0000-0000-0000-000000000001'
const TARGET_ID = '00000000-0000-0000-0000-000000000002'
const EDGE_ID = '00000000-0000-0000-0000-000000000003'

const node = {
  id: NODE_ID,
  node_type: 'agent',
  position: { x: 0, y: 0 },
}

const edge = {
  id: EDGE_ID,
  source_node_id: NODE_ID,
  target_node_id: TARGET_ID,
  edge_type: 'normal',
}

const routingEdges = [
  {
    ...edge,
    edge_type: 'loop',
    default_target: TARGET_ID,
    max_iterations: 3,
  },
  {
    ...edge,
    id: '00000000-0000-0000-0000-000000000004',
    edge_type: 'conditional',
    condition_expression: 'status == "ok"',
    routing_label: 'retry',
  },
]

describe('canonical pipeline graph schema', () => {
  it('accepts a canonical node and applies stable defaults', () => {
    const parsed = CanonicalPipelineGraphNodeSchema.parse(node)

    expect(parsed.id).toBe(NODE_ID)
    expect(parsed.idempotent).toBe(true)
    expect(parsed.join_partial_policy).toBe('collect_and_proceed')
  })

  it('accepts a canonical edge and applies port defaults', () => {
    const parsed = CanonicalPipelineGraphEdgeSchema.parse(edge)

    expect(parsed.source_port).toBe('out')
    expect(parsed.target_port).toBe('in')
  })

  it('rejects unknown graph fields instead of silently dropping them', () => {
    const result = safeValidateCanonicalGraph({
      nodes: [{ ...node, rawNodes: [] }],
      edges: [edge],
    })

    expect(result.success).toBe(false)
  })

  it('accepts loop and conditional routing edge fields', () => {
    const parsed = validateCanonicalGraph({
      nodes: [
        {
          ...node,
          routing_mode: 'llm',
          default_target: TARGET_ID,
          execution_metadata: { attempt: 2 },
          model_backend_id: 'backend-1',
          prompt_template: 'Hello {{ input.name }}',
          manual_prompt: 'Enter a value.',
          routing_prompt: 'Choose a route.',
        },
      ],
      edges: routingEdges,
    })

    expect(parsed.edges[0].max_iterations).toBe(3)
    expect(parsed.edges[1].routing_label).toBe('retry')
    expect(parsed.edges[1].edge_type).toBe('conditional')
  })

  it("rejects 'llm' as an edge_type because it is a node routing_mode, not an edge kind", () => {
    // The backend PipelineGraphEdge write model constrains edge_type to
    // normal|reject|conditional|loop; an 'llm' edge is a 422. The compiler
    // derives LLM branching from the source node's routing_mode.
    const result = safeValidateCanonicalGraph({
      nodes: [node],
      edges: [{ ...edge, edge_type: 'llm' }],
    })

    expect(result.success).toBe(false)
    if (!result.success) {
      expect(result.error.message).toContain('invalid edge_type')
    }
  })

  it('validates complete graph payloads for save requests', () => {
    const parsed = validateCanonicalGraph({ nodes: [node], edges: [edge] })

    expect(parsed.nodes).toHaveLength(1)
    expect(parsed.edges).toHaveLength(1)
  })
})
