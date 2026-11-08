import * as fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { layoutNodes, type GraphEdgeInput, type GraphNodeInput } from '../utils/graph-layout'

type PropertyGraph = {
  nodes: GraphNodeInput[]
  edges: GraphEdgeInput[]
}

const nodeIds: fc.Arbitrary<string[]> = fc.uniqueArray(fc.string({ minLength: 1, maxLength: 5 }), {
  minLength: 1,
  maxLength: 8,
})

const graphArbitrary: fc.Arbitrary<PropertyGraph> = nodeIds.chain((ids: string[]) => {
  // Sort the sampled endpoints instead of filtering. Filtering is impossible
  // for a one-node graph, and fast-check can spend its entire budget searching
  // for a pair that cannot exist.
  const edge = fc
    .tuple(fc.integer({ min: 0, max: ids.length - 1 }), fc.integer({ min: 0, max: ids.length - 1 }))
    .map(([first, second]: [number, number]): GraphEdgeInput => {
      const source = Math.min(first, second)
      const target = Math.max(first, second)
      return { source: ids[source], target: ids[target] }
    })
  return fc.record({
    nodes: fc.constant(ids.map((id: string): GraphNodeInput => ({ id }))),
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
      { numRuns: 25 },
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
      { numRuns: 25 },
    )
  })

  it('keeps each component layout independent of disconnected components', () => {
    fc.assert(
      fc.property(
        fc.tuple(graphArbitrary, graphArbitrary).filter(([a, b]: [PropertyGraph, PropertyGraph]) => {
          const ids = new Set(a.nodes.map((node: GraphNodeInput) => node.id))
          return b.nodes.every((node: GraphNodeInput) => !ids.has(node.id))
        }),
        ([a, b]: [PropertyGraph, PropertyGraph]) => {
          const aOnly = positions(layoutNodes(a.nodes, a.edges))
          const combined = positions(layoutNodes([...a.nodes, ...b.nodes], [...a.edges, ...b.edges]))
          for (const node of a.nodes) expect(combined.get(node.id)!.x).toBe(aOnly.get(node.id)!.x)
        },
      ),
      { numRuns: 25 },
    )
  })

  it('ignores a cycle back-edge when ranking a forward chain', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.string({ minLength: 1, maxLength: 5 }), { minLength: 2, maxLength: 8 }),
        (ids: string[]) => {
          const nodes = ids.map((id: string): GraphNodeInput => ({ id }))
          const forward: GraphEdgeInput[] = ids.slice(1).map((target: string, index: number) => ({
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
      { numRuns: 25 },
    )
  })

  it('re-layouts reconstructed nodes to the same deterministic result', () => {
    fc.assert(
      fc.property(graphArbitrary, ({ nodes, edges }) => {
        const arranged = layoutNodes(nodes, edges)
        const reconstructed: GraphNodeInput[] = nodesFromLayout(arranged)
        const rearranged = layoutNodes(reconstructed, edges)
        expect(rearranged).toEqual(arranged)
      }),
      { numRuns: 25 },
    )
  })
})
