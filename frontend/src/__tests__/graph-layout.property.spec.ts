import { describe, expect, it } from 'vitest'
import { fc } from '@fast-check/vitest'
import { layoutNodes, type GraphEdgeInput, type GraphNodeInput } from '../utils/graph-layout'

const nodeIds = fc.uniqueArray(fc.string({ minLength: 1, maxLength: 5 }), {
  minLength: 1,
  maxLength: 8,
})

const graphArbitrary = nodeIds.chain((ids) => {
  const edge = fc
    .tuple(fc.constantFrom(...ids), fc.constantFrom(...ids))
    .filter(([source, target]) => source !== target)
    .map(([source, target]) => ({ source, target }))
  return fc.record({
    nodes: fc.constant(ids.map((id) => ({ id }))),
    edges: fc.array(edge, { maxLength: 16 }),
  })
})

function positions(result: ReturnType<typeof layoutNodes>): Map<string, { x: number; y: number }> {
  return new Map(result.map((node) => [node.id, node.position]))
}

function nodesFromLayout(result: ReturnType<typeof layoutNodes>): GraphNodeInput[] {
  return result.map((node) => ({ id: node.id, node_type: node.type, label: node.data.label }))
}

describe('layoutNodes property invariants', () => {
  it('is idempotent when arranged output is arranged again', () => {
    fc.assert(
      fc.property(graphArbitrary, ({ nodes, edges }) => {
        const first = layoutNodes(nodes, edges)
        const second = layoutNodes(nodesFromLayout(first), edges)
        expect(second).toEqual(first)
      }),
    )
  })

  it('never places an edge source to the right of its target', () => {
    fc.assert(
      fc.property(graphArbitrary, ({ nodes, edges }) => {
        const laidOut = positions(layoutNodes(nodes, edges))
        for (const edge of edges) {
          const source = laidOut.get(edge.source)
          const target = laidOut.get(edge.target)
          if (source && target) expect(source.x).toBeLessThanOrEqual(target.x)
        }
      }),
    )
  })

  it('keeps each component layout independent of disconnected components', () => {
    fc.assert(
      fc.property(
        fc.tuple(graphArbitrary, graphArbitrary).filter(([a, b]) => {
          const ids = new Set(a.nodes.map((node) => node.id))
          return b.nodes.every((node) => !ids.has(node.id))
        }),
        ([a, b]) => {
          const aOnly = positions(layoutNodes(a.nodes, a.edges))
          const combined = positions(layoutNodes([...a.nodes, ...b.nodes], [...a.edges, ...b.edges]))
          for (const node of a.nodes) expect(combined.get(node.id)!.x).toBe(aOnly.get(node.id)!.x)
        },
      ),
    )
  })

  it('ignores a cycle back-edge when ranking a forward chain', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.string({ minLength: 1, maxLength: 5 }), { minLength: 2, maxLength: 8 }),
        (ids) => {
          const nodes = ids.map((id) => ({ id }))
          const forward: GraphEdgeInput[] = ids.slice(1).map((target, index) => ({
            source: ids[index],
            target,
          }))
          const withoutBackEdge = positions(layoutNodes(nodes, forward))
          const withBackEdge = positions(
            layoutNodes(nodes, [...forward, { source: ids[ids.length - 1], target: ids[0] }]),
          )
          for (const id of ids) expect(withBackEdge.get(id)!.x).toBe(withoutBackEdge.get(id)!.x)
        },
      ),
    )
  })

  it('preserves provenance across Arrange, undo, and re-Arrange', () => {
    fc.assert(
      fc.property(graphArbitrary, ({ nodes, edges }) => {
        const arranged = layoutNodes(nodes, edges)
        const undone: GraphNodeInput[] = nodes.map((node) => ({ ...node }))
        const rearranged = layoutNodes(undone, edges)
        expect(rearranged).toEqual(arranged)
      }),
    )
  })
})
