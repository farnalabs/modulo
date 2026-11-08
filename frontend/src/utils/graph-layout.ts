export interface GraphNodeInput {
  id: string
  node_type?: string
  label?: string
  /** Optional measured width; falls back to `options.nodeWidth`. */
  width?: number
  /** Optional measured height; falls back to `options.nodeHeight`. */
  height?: number
  /** Current position, used by incremental layout to calculate user offsets. */
  position?: { x: number; y: number }
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
  /** Vertical gap between disconnected components. */
  componentGap?: number
}

const DEFAULT_NODE_WIDTH = 200
const DEFAULT_NODE_HEIGHT = 100
const DEFAULT_X_PAD = 40
const DEFAULT_Y_PAD = 60

interface ResolvedOptions {
  nodeWidth: number
  nodeHeight: number
  xPad: number
  yPad: number
  componentGap: number
}

interface ValidEdge {
  source: string
  target: string
}

function resolveOptions(options: LayoutOptions): ResolvedOptions {
  const nodeWidth = options.nodeWidth ?? DEFAULT_NODE_WIDTH
  const nodeHeight = options.nodeHeight ?? DEFAULT_NODE_HEIGHT
  const yPad = options.yPad ?? DEFAULT_Y_PAD
  return {
    nodeWidth,
    nodeHeight,
    xPad: options.xPad ?? DEFAULT_X_PAD,
    yPad,
    componentGap: options.componentGap ?? nodeHeight + yPad,
  }
}

function nodeWidth(node: GraphNodeInput, options: ResolvedOptions): number {
  return node.width ?? options.nodeWidth
}

function nodeHeight(node: GraphNodeInput, options: ResolvedOptions): number {
  return node.height ?? options.nodeHeight
}

/** Edges whose endpoints both exist; self-loops are irrelevant to ranking. */
function validEdges(ids: Set<string>, edges: GraphEdgeInput[]): ValidEdge[] {
  const result: ValidEdge[] = []
  for (const edge of edges) {
    if (!ids.has(edge.source) || !ids.has(edge.target)) continue
    result.push({ source: edge.source, target: edge.target })
  }
  return result
}

function edgeKey(source: string, target: string): string {
  return `${source}\u0000${target}`
}

/**
 * DFS back-edge removal. Removing every edge that points at a node still on
 * the DFS stack always leaves a DAG, so a cyclic graph cannot make the rank
 * pass loop or push ranks backwards indefinitely.
 */
function findBackEdges(ids: string[], adjacency: Map<string, string[]>): Set<string> {
  const WHITE = 0
  const GREY = 1
  const BLACK = 2
  const colour = new Map<string, number>(ids.map((id) => [id, WHITE]))
  const backEdges = new Set<string>()

  const visit = (id: string): void => {
    colour.set(id, GREY)
    for (const target of adjacency.get(id)!) {
      const targetColour = colour.get(target)!
      if (targetColour === GREY) {
        backEdges.add(edgeKey(id, target))
      } else if (targetColour === WHITE) {
        visit(target)
      }
    }
    colour.set(id, BLACK)
  }

  for (const id of ids) {
    if (colour.get(id) === WHITE) visit(id)
  }
  return backEdges
}

/** Kahn topological order seeded by input order, so output is deterministic. */
function topologicalOrder(ids: string[], adjacency: Map<string, string[]>): string[] {
  const inDegree = new Map<string, number>(ids.map((id) => [id, 0]))
  for (const id of ids) {
    for (const target of adjacency.get(id)!) {
      inDegree.set(target, inDegree.get(target)! + 1)
    }
  }

  const queue = ids.filter((id) => inDegree.get(id)! === 0)
  let head = 0
  while (head < queue.length) {
    const id = queue[head++]
    for (const target of adjacency.get(id)!) {
      const remaining = inDegree.get(target)! - 1
      inDegree.set(target, remaining)
      if (remaining === 0) queue.push(target)
    }
  }
  return queue
}

/**
 * Longest-path rank: `rank(node) = max(rank(predecessor)) + 1`. A join is
 * therefore always placed after its deepest predecessor.
 */
function rankByLongestPath(ordered: string[], precedence: Map<string, string[]>): Map<string, number> {
  const rank = new Map<string, number>(ordered.map((id) => [id, 0]))
  for (const id of ordered) {
    for (const target of precedence.get(id)!) {
      rank.set(target, Math.max(rank.get(target)!, rank.get(id)! + 1))
    }
  }
  return rank
}

function groupByRank(ordered: string[], rank: Map<string, number>): Map<number, string[]> {
  const byRank = new Map<number, string[]>()
  for (const id of ordered) {
    const r = rank.get(id)!
    const bucket = byRank.get(r)
    if (bucket) bucket.push(id)
    else byRank.set(r, [id])
  }
  return byRank
}

/**
 * Barycentre ordering within a rank: each node is sorted by the average
 * position of its predecessors in the previous rank. Ties fall back to the
 * input order so equal graphs always lay out identically.
 */
function orderRankByBarycentre(
  nodes: string[],
  previous: string[],
  edges: ValidEdge[],
  inputIndex: Map<string, number>,
): string[] {
  const previousIndex = new Map(previous.map((id, index) => [id, index]))
  const barycentre = (node: string): number => {
    let total = 0
    let count = 0
    for (const edge of edges) {
      if (edge.target !== node) continue
      const index = previousIndex.get(edge.source)
      if (index === undefined) continue
      total += index
      count += 1
    }
    // count 0 yields NaN (no predecessor in the previous rank); the sort
    // below treats that as a tie and falls back to input order, so equal
    // graphs always produce the same branch order.
    return total / count
  }
  return [...nodes].sort((a, b) => {
    const delta = barycentre(a) - barycentre(b)
    return delta || inputIndex.get(a)! - inputIndex.get(b)!
  })
}

interface LocalPosition {
  x: number
  y: number
  height: number
}

/** Lay out one weakly connected component, anchored at (0, 0). */
function layoutComponent(
  ids: string[],
  edges: ValidEdge[],
  nodesById: Map<string, GraphNodeInput>,
  options: ResolvedOptions,
  inputIndex: Map<string, number>,
): { positions: Map<string, LocalPosition>; height: number } {
  const idSet = new Set(ids)
  const componentEdges = edges.filter((edge) => idSet.has(edge.source) && idSet.has(edge.target))

  const adjacency = new Map<string, string[]>(ids.map((id) => [id, []]))
  for (const edge of componentEdges) {
    adjacency.get(edge.source)!.push(edge.target)
  }

  const backEdges = findBackEdges(ids, adjacency)
  const precedence = new Map<string, string[]>(ids.map((id) => [id, []]))
  for (const edge of componentEdges) {
    if (backEdges.has(edgeKey(edge.source, edge.target))) continue
    precedence.get(edge.source)!.push(edge.target)
  }

  const ordered = topologicalOrder(ids, precedence)
  const rank = rankByLongestPath(ordered, precedence)
  const byRank = groupByRank(ordered, rank)
  const ranks = [...byRank.keys()].sort((a, b) => a - b)

  for (let index = 1; index < ranks.length; index += 1) {
    const previous = byRank.get(ranks[index - 1])!
    byRank.set(
      ranks[index],
      orderRankByBarycentre(byRank.get(ranks[index])!, previous, componentEdges, inputIndex),
    )
  }

  const rankWidths = new Map<number, number>()
  for (const r of ranks) {
    let width = 0
    for (const id of byRank.get(r)!) {
      width = Math.max(width, nodeWidth(nodesById.get(id)!, options))
    }
    rankWidths.set(r, width)
  }

  const positions = new Map<string, LocalPosition>()
  let x = 0
  let componentHeight = 0
  for (const r of ranks) {
    let y = 0
    for (const id of byRank.get(r)!) {
      const height = nodeHeight(nodesById.get(id)!, options)
      positions.set(id, { x, y, height })
      y += height + options.yPad
    }
    componentHeight = Math.max(componentHeight, Math.max(0, y - options.yPad))
    x += rankWidths.get(r)! + options.xPad
  }

  return { positions, height: componentHeight }
}

/** Weakly connected components in input order. */
function connectedComponents(ids: string[], edges: ValidEdge[]): string[][] {
  const neighbours = new Map<string, string[]>(ids.map((id) => [id, []]))
  for (const edge of edges) {
    neighbours.get(edge.source)!.push(edge.target)
    neighbours.get(edge.target)!.push(edge.source)
  }

  const seen = new Set<string>()
  const components: string[][] = []
  for (const id of ids) {
    if (seen.has(id)) continue
    const members = new Set<string>()
    const stack = [id]
    seen.add(id)
    while (stack.length > 0) {
      const current = stack.pop()!
      members.add(current)
      for (const neighbour of neighbours.get(current)!) {
        if (seen.has(neighbour)) continue
        seen.add(neighbour)
        stack.push(neighbour)
      }
    }
    components.push(ids.filter((candidate) => members.has(candidate)))
  }
  return components
}

/**
 * Deterministic left-to-right layered (Sugiyama-style) layout for a pipeline
 * graph.
 *
 * - Chains rank successively on the X axis; joins sit after their deepest
 *   predecessor (longest-path ranking).
 * - Ranks are ordered by predecessor barycentre, with input order as the tie
 *   break, so branches are stable across runs.
 * - Weakly connected components are laid out independently and stacked with a
 *   gap, so disconnected graphs cannot interleave or overlap.
 * - Cycle-closing (back) edges are ignored for ranking only; every node still
 *   receives a position, so loops cannot hang the layout or pull the forward
 *   graph backwards.
 *
 * Returns nodes in input order with presentation-only positions.
 */
export function layoutNodes(
  nodes: GraphNodeInput[],
  edges: GraphEdgeInput[],
  options: LayoutOptions = {},
): LaidOutNode[] {
  if (nodes.length === 0) return []

  const resolved = resolveOptions(options)
  const nodesById = new Map(nodes.map((node) => [node.id, node]))
  const ids = nodes.map((node) => node.id)
  const idSet = new Set(ids)
  const inputIndex = new Map(ids.map((id, index) => [id, index]))
  const valid = validEdges(idSet, edges)
  const components = connectedComponents(ids, valid)

  const positions = new Map<string, { x: number; y: number }>()
  let yOffset = 0
  for (const component of components) {
    const local = layoutComponent(component, valid, nodesById, resolved, inputIndex)
    for (const [id, position] of local.positions) {
      positions.set(id, { x: position.x, y: yOffset + position.y })
    }
    yOffset += local.height + resolved.componentGap
  }

  return nodes.map((node) => ({
    id: node.id,
    // node_type passes through unchanged, so a `dispatch` node keeps its
    // own canvas type (FAR-1141) instead of collapsing into the generic
    // `agent` node; only an ABSENT node_type falls back to `agent`.
    type: node.node_type || 'agent',
    position: positions.get(node.id)!,
    data: { label: node.label || node.id },
  }))
}

export interface ProvenanceMap {
  [nodeId: string]: 'user' | 'generated'
}

/** Result of an incremental layout: final nodes plus the pre-delta baseline. */
export interface IncrementalLayoutResult {
  /** Nodes with user offsets applied, in input order. */
  nodes: LaidOutNode[]
  /**
   * The fresh generated positions BEFORE any user offsets are applied. Callers
   * must store this as the next incremental layout's `previousPositions`;
   * storing the post-delta positions instead makes the next Arrange compute a
   * zero delta and snap user-offset nodes back onto the generated layout.
   */
  freshPositions: Record<string, { x: number; y: number }>
}

/**
 * Incremental layout that preserves user manual drag offsets.
 *
 * Computes a new layout from scratch, then for each node with provenance='user',
 * computes the delta from the previous generated layout and applies it to the
 * new layout. This preserves the user's relative manual positioning while
 * benefiting from the improved algorithmic layout.
 *
 * Returns both the final nodes and the pre-delta fresh baseline so callers can
 * keep the baseline (not the post-delta positions) for the next Arrange.
 */
export function layoutNodesIncrementalWithBaseline(
  nodes: GraphNodeInput[],
  edges: GraphEdgeInput[],
  previousPositions: Record<string, { x: number; y: number }>,
  userProvenance: ProvenanceMap,
  options: LayoutOptions = {},
): IncrementalLayoutResult {
  // First, compute the fresh layout
  const layoutOptions = options
  const freshLaidOut = layoutNodes(nodes, edges, layoutOptions)
  const freshPositions = new Map(freshLaidOut.map((node) => [node.id, node.position]))

  // Compute deltas for user-positioned nodes from the previous generated layout
  const userDeltas = new Map<string, { x: number; y: number }>()
  for (const node of nodes) {
    if (userProvenance[node.id] === 'user') {
      const prevPos = previousPositions[node.id]
      const freshPos = freshPositions.get(node.id)
      const currentPos = node.position
      if (prevPos && freshPos && currentPos) {
        userDeltas.set(node.id, {
          x: currentPos.x - prevPos.x,
          y: currentPos.y - prevPos.y,
        })
      }
    }
  }

  // Apply deltas to user-positioned nodes in the fresh layout
  const result: LaidOutNode[] = freshLaidOut.map((node) => {
    const delta = userDeltas.get(node.id)
    if (delta) {
      return {
        ...node,
        position: {
          x: node.position.x + delta.x,
          y: node.position.y + delta.y,
        },
      }
    }
    return node
  })

  return {
    nodes: result,
    freshPositions: Object.fromEntries(
      Array.from(freshPositions, ([id, position]) => [id, { ...position }]),
    ),
  }
}

/**
 * Incremental layout that preserves user manual drag offsets.
 *
 * Convenience wrapper over {@link layoutNodesIncrementalWithBaseline} that
 * returns only the final nodes. Use the `WithBaseline` variant when the caller
 * must persist the pre-delta baseline for a subsequent Arrange.
 */
export function layoutNodesIncremental(
  nodes: GraphNodeInput[],
  edges: GraphEdgeInput[],
  previousPositions: Record<string, { x: number; y: number }>,
  userProvenance: ProvenanceMap,
  options: LayoutOptions = {},
): LaidOutNode[] {
  return layoutNodesIncrementalWithBaseline(
    nodes,
    edges,
    previousPositions,
    userProvenance,
    options,
  ).nodes
}
