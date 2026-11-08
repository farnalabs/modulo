import { describe, it, expect } from 'vitest'
import {
  layoutNodes,
  layoutNodesIncremental,
  type GraphEdgeInput,
  type GraphNodeInput,
} from '../utils/graph-layout'

function byId<T extends { id: string }>(nodes: T[]): Record<string, T> {
  return Object.fromEntries(nodes.map((node) => [node.id, node]))
}

function rectanglesOverlap(
  a: { position: { x: number; y: number } },
  b: { position: { x: number; y: number } },
  width = 200,
  height = 100,
): boolean {
  return (
    a.position.x < b.position.x + width &&
    b.position.x < a.position.x + width &&
    a.position.y < b.position.y + height &&
    b.position.y < a.position.y + height
  )
}

describe('layoutNodes', () => {
  it('lays out a single node at the origin', () => {
    const result = layoutNodes([{ id: 'a' }], [])
    expect(result).toEqual([
      { id: 'a', type: 'agent', position: { x: 0, y: 0 }, data: { label: 'a' } },
    ])
  })

  it('returns no nodes for an empty graph', () => {
    expect(layoutNodes([], [])).toEqual([])
  })

  it('places a chain left to right on successive ranks', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
      [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }],
    )
    const nodes = byId(result)
    expect(nodes.a.position).toEqual({ x: 0, y: 0 })
    expect(nodes.b.position).toEqual({ x: 240, y: 0 })
    expect(nodes.c.position).toEqual({ x: 480, y: 0 })
  })

  it('places a diamond with both branches on one rank and the join after them', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }],
      [
        { source: 'a', target: 'b' },
        { source: 'a', target: 'c' },
        { source: 'b', target: 'd' },
        { source: 'c', target: 'd' },
      ],
    )
    const nodes = byId(result)
    expect(nodes.a.position).toEqual({ x: 0, y: 0 })
    expect(nodes.b.position).toEqual({ x: 240, y: 0 })
    expect(nodes.c.position).toEqual({ x: 240, y: 160 })
    expect(nodes.d.position).toEqual({ x: 480, y: 0 })
  })

  it('places a join after its deepest predecessor, not its shallowest', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'join' }],
      [
        { source: 'a', target: 'b' },
        { source: 'b', target: 'c' },
        { source: 'a', target: 'join' },
        { source: 'c', target: 'join' },
      ],
    )
    const nodes = byId(result)
    expect(nodes.c.position.x).toBe(480)
    expect(nodes.join.position.x).toBe(720)
  })

  it('separates disconnected components vertically instead of interleaving them', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }, { id: 'lonely' }],
      [{ source: 'a', target: 'b' }],
    )
    const nodes = byId(result)
    expect(nodes.a.position).toEqual({ x: 0, y: 0 })
    expect(nodes.b.position).toEqual({ x: 240, y: 0 })
    expect(nodes.lonely.position).toEqual({ x: 0, y: 260 })
  })

  it('never overlaps nodes in ordinary, branching or disconnected layouts', () => {
    const nodes: GraphNodeInput[] = [
      { id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }, { id: 'e' },
    ]
    const edges: GraphEdgeInput[] = [
      { source: 'a', target: 'b' },
      { source: 'a', target: 'c' },
      { source: 'b', target: 'd' },
      { source: 'c', target: 'd' },
    ]
    const laidOut = layoutNodes(nodes, edges)
    for (let i = 0; i < laidOut.length; i += 1) {
      for (let j = i + 1; j < laidOut.length; j += 1) {
        expect(
          rectanglesOverlap(laidOut[i], laidOut[j]),
          `${laidOut[i].id} overlaps ${laidOut[j].id}`,
        ).toBe(false)
      }
    }
  })

  it('handles cycles without hanging and keeps the forward chain left to right', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
      [
        { source: 'a', target: 'b' },
        { source: 'b', target: 'c' },
        { source: 'c', target: 'a' },
      ],
    )
    const nodes = byId(result)
    expect(result).toHaveLength(3)
    expect(nodes.a.position.x).toBe(0)
    expect(nodes.b.position.x).toBe(240)
    expect(nodes.c.position.x).toBe(480)
  })

  it('handles a two-node loop and a self-loop', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }],
      [
        { source: 'a', target: 'b' },
        { source: 'b', target: 'a' },
        { source: 'a', target: 'a' },
      ],
    )
    const nodes = byId(result)
    expect(nodes.a.position.x).toBe(0)
    expect(nodes.b.position.x).toBe(240)
  })

  it('breaks a same-rank cycle deterministically by input order', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
      [
        { source: 'a', target: 'c' },
        { source: 'c', target: 'b' },
        { source: 'b', target: 'a' },
      ],
    )
    const nodes = byId(result)
    expect(nodes.a.position.x).toBe(0)
    expect(nodes.c.position.x).toBe(240)
    expect(nodes.b.position.x).toBe(480)
  })

  it('is deterministic across repeated runs', () => {
    const nodes: GraphNodeInput[] = [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }]
    const edges: GraphEdgeInput[] = [
      { source: 'a', target: 'b' },
      { source: 'a', target: 'c' },
      { source: 'b', target: 'd' },
      { source: 'c', target: 'd' },
    ]
    expect(layoutNodes(nodes, edges)).toEqual(layoutNodes(nodes, edges))
  })

  it('orders a later rank by predecessor barycentre, not input order', () => {
    const result = layoutNodes(
      [{ id: 'root' }, { id: 'x' }, { id: 'y' }, { id: 'p' }, { id: 'q' }],
      [
        { source: 'root', target: 'x' },
        { source: 'root', target: 'y' },
        { source: 'y', target: 'p' },
        { source: 'x', target: 'q' },
      ],
    )
    const nodes = byId(result)
    // q follows x (rank-1 index 0) and p follows y (index 1), so q is placed
    // above p even though p has the lower input index.
    expect(nodes.q.position.y).toBe(0)
    expect(nodes.p.position.y).toBe(160)
  })

  it('ignores predecessors from earlier ranks when ordering a rank by barycentre', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }],
      [
        { source: 'a', target: 'b' },
        { source: 'b', target: 'c' },
        { source: 'b', target: 'd' },
        { source: 'a', target: 'd' },
      ],
    )
    const nodes = byId(result)
    // c and d share rank 2; only b is in the previous rank, so the skip-level
    // a -> d edge is ignored for ordering. c precedes d by input order.
    expect(nodes.c.position).toEqual({ x: 480, y: 0 })
    expect(nodes.d.position).toEqual({ x: 480, y: 160 })
  })

  it('sizes ranks and rows from per-node dimensions, without overlap', () => {
    const result = layoutNodes(
      [
        { id: 'wide', width: 320, height: 120 },
        { id: 'narrow', width: 100, height: 60 },
        { id: 'tail', width: 200, height: 100 },
      ],
      [
        { source: 'wide', target: 'tail' },
        { source: 'narrow', target: 'tail' },
      ],
    )
    const nodes = byId(result)
    expect(nodes.wide.position.x).toBe(0)
    expect(nodes.narrow.position.x).toBe(0)
    expect(nodes.wide.position.y).toBe(0)
    expect(nodes.narrow.position.y).toBe(180)
    expect(nodes.tail.position.x).toBe(360)
  })

  it('ignores edges that reference unknown nodes', () => {
    const result = layoutNodes(
      [{ id: 'a' }],
      [{ source: 'a', target: 'ghost' }, { source: 'ghost', target: 'a' }],
    )
    expect(result).toHaveLength(1)
    expect(result[0].position).toEqual({ x: 0, y: 0 })
  })

  it('uses node_type and label from the input when provided', () => {
    const result = layoutNodes([{ id: 'a', node_type: 'schema', label: 'Schema A' }], [])
    expect(result[0]).toMatchObject({ type: 'schema', data: { label: 'Schema A' } })
  })

  it('preserves input order in the returned nodes', () => {
    const result = layoutNodes(
      [{ id: 'c' }, { id: 'a' }, { id: 'b' }],
      [{ source: 'a', target: 'b' }],
    )
    expect(result.map((node) => node.id)).toEqual(['c', 'a', 'b'])
  })

  it('respects custom spacing options', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }],
      [{ source: 'a', target: 'b' }],
      { nodeWidth: 100, nodeHeight: 50, xPad: 20, yPad: 30 },
    )
    expect(result[1].position).toEqual({ x: 120, y: 0 })
  })
})

describe('layoutNodesIncremental', () => {
  it('preserves manual offsets from the previous generated layout', () => {
    const nodes: GraphNodeInput[] = [
      { id: 'a', position: { x: 50, y: 25 } },
      { id: 'b', position: { x: 240, y: 0 } },
    ]
    const edges: GraphEdgeInput[] = [{ source: 'a', target: 'b' }]
    const previousGenerated = {
      a: { x: 0, y: 0 },
      b: { x: 240, y: 0 },
    }

    const result = layoutNodesIncremental(nodes, edges, previousGenerated, { a: 'user', b: 'generated' })
    const byNode = byId(result)

    expect(byNode.a.position).toEqual({ x: 50, y: 25 })
    expect(byNode.b.position).toEqual({ x: 240, y: 0 })
  })

  it('recomputes generated nodes while preserving unchanged user positions', () => {
    const nodes: GraphNodeInput[] = [
      { id: 'a', position: { x: 30, y: 10 } },
      { id: 'b', position: { x: 240, y: 0 } },
      { id: 'c', position: { x: 480, y: 0 } },
    ]
    const edges: GraphEdgeInput[] = [
      { source: 'a', target: 'b' },
      { source: 'b', target: 'c' },
    ]
    const previousGenerated = {
      a: { x: 0, y: 0 },
      b: { x: 240, y: 0 },
      c: { x: 480, y: 0 },
    }

    const result = layoutNodesIncremental(nodes, edges, previousGenerated, { a: 'user', b: 'generated', c: 'generated' })
    const byNode = byId(result)

    expect(byNode.a.position).toEqual({ x: 30, y: 10 })
    expect(byNode.b.position).toEqual({ x: 240, y: 0 })
    expect(byNode.c.position).toEqual({ x: 480, y: 0 })
  })

  it('falls back to the fresh layout for user nodes without a previous generated position', () => {
    const nodes: GraphNodeInput[] = [{ id: 'a', position: { x: 120, y: 80 } }]
    const result = layoutNodesIncremental(nodes, [], {}, { a: 'user' })

    expect(result[0].position).toEqual({ x: 0, y: 0 })
  })
})
