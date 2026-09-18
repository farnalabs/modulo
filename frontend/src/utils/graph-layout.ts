export interface GraphNodeInput {
  id: string
  node_type?: string
  label?: string
}

export interface GraphEdgeInput {
  source: string
  target: string
  edge_type?: string
}

export interface LaidOutNode {
  id: string
  type: string
  position: { x: number; y: number }
  data: { label: string }
}

export interface LayoutOptions {
  nodeWidth?: number
  nodeHeight?: number
  xPad?: number
  yPad?: number
}

export function layoutNodes(
  nodes: GraphNodeInput[],
  edges: GraphEdgeInput[],
  options: LayoutOptions = {},
): LaidOutNode[] {
  const w = options.nodeWidth ?? 200
  const h = options.nodeHeight ?? 100
  const xPad = options.xPad ?? 40
  const yPad = options.yPad ?? 60

  const inDegree: Record<string, number> = {}
  for (const n of nodes) inDegree[n.id] = 0
  for (const e of edges) inDegree[e.target] = (inDegree[e.target] || 0) + 1

  const layers: string[][] = []
  const remaining = new Set(nodes.map(n => n.id))
  while (remaining.size > 0) {
    const layer = [...remaining].filter(id => inDegree[id] === 0 || [...remaining].every(other => {
      if (other === id) return true
      return !edges.some(e => e.source === other && e.target === id)
    }))
    if (layer.length === 0) {
      layers.push([...remaining])
      break
    }
    layers.push(layer)
    for (const id of layer) remaining.delete(id)
    for (const id of remaining) {
      inDegree[id] = edges.filter(e => e.target === id && !remaining.has(e.source)).length
    }
  }

  const nodeMap = new Map(nodes.map(n => [n.id, n]))
  return layers.flatMap((layer, li) =>
    layer.map((id, ni) => {
      const n = nodeMap.get(id)
      const total = layer.length
      const startX = (total - 1) * (w + xPad) / -2
      return {
        id,
        type: n?.node_type || 'agent',
        position: { x: startX + ni * (w + xPad), y: li * (h + yPad) },
        data: { label: n?.label || id },
      }
    }),
  )
}
