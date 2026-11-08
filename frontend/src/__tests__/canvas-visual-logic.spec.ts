import { describe, expect, it } from 'vitest'
import { layoutNodes, type GraphEdgeInput, type GraphNodeInput } from '../utils/graph-layout'

const CANVAS_NODES: GraphNodeInput[] = [
  { id: 'manual', node_type: 'manual', label: 'Manual input' },
  { id: 'agent', node_type: 'agent', label: 'Agent' },
  { id: 'router', node_type: 'router', label: 'Router' },
  { id: 'hitl', node_type: 'hitl', label: 'Human review' },
  { id: 'dispatch', node_type: 'dispatch', label: 'Dispatch' },
  { id: 'connector', node_type: 'connector', label: 'Connector' },
]

const CANVAS_EDGES: GraphEdgeInput[] = [
  { source: 'manual', target: 'agent' },
  { source: 'agent', target: 'router' },
  { source: 'router', target: 'hitl' },
  { source: 'router', target: 'dispatch' },
  { source: 'hitl', target: 'connector' },
  { source: 'dispatch', target: 'connector' },
]

function byId<T extends { id: string }>(items: T[]): Record<string, T> {
  return Object.fromEntries(items.map((item) => [item.id, item]))
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

function canvasSvgFixture(): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.innerHTML = `
    <defs>
      <marker id="canvas-arrow" markerWidth="16" markerHeight="16" refX="8" refY="8" orient="auto">
        <path d="M0,0 L8,8 L0,16 Z" fill="currentColor" />
      </marker>
    </defs>
    <path class="canvas-edge" marker-end="url(#canvas-arrow)" />
    <g class="canvas-node canvas-node-router"><rect class="border-dashed" /><text>router</text></g>
    <g class="canvas-node canvas-node-hitl"><rect class="border-double" /><text>human review</text></g>
    <g class="canvas-node canvas-node-dispatch"><rect class="border-cyan" /><text>dispatch</text></g>
    <g class="canvas-node canvas-node-connector"><rect class="border-teal" /><text>connector</text></g>
  `
  return svg
}

describe('canvas visual logic', () => {
  it('produces a stable, non-overlapping six-type canvas layout', () => {
    const result = layoutNodes(CANVAS_NODES, CANVAS_EDGES)

    expect(result).toMatchInlineSnapshot(`
      [
        {
          "data": {
            "label": "Manual input",
          },
          "id": "manual",
          "position": {
            "x": 0,
            "y": 0,
          },
          "type": "manual",
        },
        {
          "data": {
            "label": "Agent",
          },
          "id": "agent",
          "position": {
            "x": 240,
            "y": 0,
          },
          "type": "agent",
        },
        {
          "data": {
            "label": "Router",
          },
          "id": "router",
          "position": {
            "x": 480,
            "y": 0,
          },
          "type": "router",
        },
        {
          "data": {
            "label": "Human review",
          },
          "id": "hitl",
          "position": {
            "x": 720,
            "y": 0,
          },
          "type": "hitl",
        },
        {
          "data": {
            "label": "Dispatch",
          },
          "id": "dispatch",
          "position": {
            "x": 720,
            "y": 160,
          },
          "type": "dispatch",
        },
        {
          "data": {
            "label": "Connector",
          },
          "id": "connector",
          "position": {
            "x": 960,
            "y": 0,
          },
          "type": "connector",
        },
      ]
    `)

    for (let i = 0; i < result.length; i += 1) {
      for (let j = i + 1; j < result.length; j += 1) {
        expect(rectanglesOverlap(result[i], result[j]), `${result[i].id} overlaps ${result[j].id}`).toBe(false)
      }
    }
  })

  it('keeps the six canvas node types and directional edge contract visible in SVG', () => {
    const svg = canvasSvgFixture()
    document.body.append(svg)

    expect(svg.querySelectorAll('.canvas-node')).toHaveLength(4)
    expect(svg.querySelector('.canvas-node-router')?.querySelector('.border-dashed')).not.toBeNull()
    expect(svg.querySelector('.canvas-node-hitl')?.querySelector('.border-double')).not.toBeNull()
    expect(svg.querySelector('.canvas-node-dispatch')?.querySelector('.border-cyan')).not.toBeNull()
    expect(svg.querySelector('.canvas-node-connector')?.querySelector('.border-teal')).not.toBeNull()
    expect(svg.querySelector('.canvas-edge')?.getAttribute('marker-end')).toBe('url(#canvas-arrow)')

    // jsdom preserves the fixture's source indentation, so the snapshot is
    // taken over a whitespace-collapsed form — the contract being pinned is
    // the structure and the attributes, not jsdom's serialisation formatting.
    const normalised = svg.outerHTML.replace(/>\s+</g, '><').trim()
    expect(normalised).toMatchInlineSnapshot(`
      "<svg><defs><marker id=\"canvas-arrow\" markerWidth=\"16\" markerHeight=\"16\" refX=\"8\" refY=\"8\" orient=\"auto\"><path d=\"M0,0 L8,8 L0,16 Z\" fill=\"currentColor\"></path></marker></defs><path class=\"canvas-edge\" marker-end=\"url(#canvas-arrow)\"></path><g class=\"canvas-node canvas-node-router\"><rect class=\"border-dashed\"></rect><text>router</text></g><g class=\"canvas-node canvas-node-hitl\"><rect class=\"border-double\"></rect><text>human review</text></g><g class=\"canvas-node canvas-node-dispatch\"><rect class=\"border-cyan\"></rect><text>dispatch</text></g><g class=\"canvas-node canvas-node-connector\"><rect class=\"border-teal\"></rect><text>connector</text></g></svg>"
    `)

    svg.remove()
  })

  it('ignores invalid edges without changing the logical layout', () => {
    const valid = layoutNodes(CANVAS_NODES, CANVAS_EDGES)
    const withInvalidEdges = layoutNodes(CANVAS_NODES, [
      ...CANVAS_EDGES,
      { source: 'ghost', target: 'agent' },
      { source: 'connector', target: 'ghost' },
    ])

    expect(withInvalidEdges).toEqual(valid)
    expect(byId(withInvalidEdges).connector.position).toEqual({ x: 960, y: 0 })
  })
})
