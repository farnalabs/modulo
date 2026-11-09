/** FAR-2475: Typed frontend projection adapters from generated OpenAPI graph types.
 *
 * These adapters map the full generated `components['schemas']['PipelineGraphNode']`
 * and `PipelineGraphEdge` to smaller canvas/layout types used by the editor.
 *
 * UI-only layout fields (selection, drag state, computed dimensions) are kept
 * SEPARATE in `CanvasNodeUiState` / `CanvasEdgeUiState` and are NEVER sourced
 * from the wire payload.
 *
 * Consumes generated types from `../lib/api/schema` - does NOT hand-duplicate
 * the API shape.
 */

import type { components } from '@/lib/api/schema'

/** ========================================================================
 *  Wire types (generated from OpenAPI) - re-exported for convenience
 *  ========================================================================
 */

export type PipelineGraphNodeWire = components['schemas']['PipelineGraphNode']
export type PipelineGraphEdgeWire = components['schemas']['PipelineGraphEdge']
export type PipelineGraphResponseWire = components['schemas']['PipelineGraphResponse']
export type PipelineGraphUpdateWire = components['schemas']['PipelineGraphUpdate']

/** ========================================================================
 *  Canvas projection types (semantic subset of wire types)
 *  ========================================================================
 */

/** Canvas node types that exist as distinct visual cards. */
export type CanvasNodeType =
  | 'manual'
  | 'agent'
  | 'router'
  | 'hitl'
  | 'dispatch'
  | 'connector'

/** Mapping from backend node_type to canvas type. */
export const CANVAS_TYPE_BY_BACKEND_TYPE: Record<string, CanvasNodeType> = {
  manual: 'manual',
  router: 'router',
  hitl: 'hitl',
  dispatch: 'dispatch',
  connector: 'connector',
}

/** @deprecated Use CANVAS_TYPE_BY_BACKEND_TYPE directly. */
export function getCanvasType(backendType: string): CanvasNodeType {
  return CANVAS_TYPE_BY_BACKEND_TYPE[backendType] ?? 'agent'
}

/** Position on the canvas. */
export interface CanvasPosition {
  x: number
  y: number
}

/** Semantic data carried by a canvas node - ONLY fields derived from the
 * wire contract. UI-only fields (selection, drag state, computed size)
 * are in {@link CanvasNodeUiState} and are NEVER sourced from the API. */
export interface CanvasNodeData {
  /** Human-readable label. */
  label: string
  /** Description for tooltips/inspectors. */
  description: string
  /** Original backend node_type (for logic that depends on it). */
  node_type: PipelineGraphNodeWire['node_type']
  /** HITL gate config (edge-level or node-level). */
  hitl_config: PipelineGraphNodeWire['hitl_config'] | null
  /** Policy gate declaration. */
  policy_gate: PipelineGraphNodeWire['policy_gate'] | null
  /** Parameter set reference. */
  parameter_set_id: string | null
  /** Parameter overrides. */
  parameter_overrides: Record<string, unknown> | null
  /** Whether the node declares a capability scope. */
  hasCapabilityScope: boolean
  /** Connector binding (query/write/dispatch). */
  connector_binding: PipelineGraphNodeWire['connector_binding'] | null
  /** Router configuration. */
  router_config: PipelineGraphNodeWire['router_config'] | null
}

/** Full canvas node projection: wire payload -> semantic + position. */
export interface CanvasNodeProjection {
  id: string
  type: CanvasNodeType
  position: CanvasPosition
  data: CanvasNodeData
}

/** UI-only canvas state, owned by the editor and NEVER sourced from the wire.
 * Fields here are deliberately absent from {@link CanvasNodeProjection}. */
export interface CanvasNodeUiState {
  /** Whether the node is currently selected. */
  selected: boolean
  /** Whether the node is being dragged. */
  dragging: boolean
  /** Computed width of the rendered card (pixels). */
  width?: number
  /** Computed height of the rendered card (pixels). */
  height?: number
  /** Z-index for overlay stacking. */
  zIndex?: number
}

/** Default UI state (fresh node, not selected, not dragging). */
export const defaultCanvasNodeUiState: CanvasNodeUiState = {
  selected: false,
  dragging: false,
}

/** Edge projection: wire payload -> semantic + geometry. */
export interface CanvasEdgeProjection {
  id: string
  source: string
  target: string
  /** Semantic edge type. */
  edgeType: 'normal' | 'reject' | 'conditional' | 'loop'
  /** HITL review config on the edge. */
  hitl_review_config: PipelineGraphEdgeWire['hitl_review_config'] | null
  /** Conditional edge expression. */
  conditionExpression: string | null
  /** Source port. */
  sourcePort: string
  /** Target port. */
  targetPort: string
}

/** UI-only edge styling (animated, stroke, marker). NEVER from wire. */
export interface CanvasEdgeUiState {
  animated: boolean
  stroke: string
  style: Record<string, string>
  markerEnd: {
    type: 'ArrowClosed'
    width: number
    height: number
    color: string
  }
}

/** Full graph projection returned by the adapter. */
export interface CanvasGraphProjection {
  nodes: CanvasNodeProjection[]
  edges: CanvasEdgeProjection[]
}

/** ========================================================================
 *  Projection adapters (pure functions, no side effects)
 *  ========================================================================
 */

/** Map a single wire node to its canvas projection. */
export function projectGraphNode(node: PipelineGraphNodeWire): CanvasNodeProjection {
  const nodeType = CANVAS_TYPE_BY_BACKEND_TYPE[node.node_type] ?? 'agent'

  // HitL config can be at node.hitl_config (node-level) OR on incoming edge
  // (edge-level). The projection carries node.hitl_config; edge config is
  // attached separately in projectGraph.
  return {
    id: node.id,
    type: nodeType,
    position: { x: node.position.x, y: node.position.y },
    data: {
      label: node.label ?? `Node ${shortId(node.id)}`,
      description: node.description ?? '',
      node_type: node.node_type,
      hitl_config: node.hitl_config ?? null,
      policy_gate: node.policy_gate ?? null,
      parameter_set_id: node.parameter_set_id ?? null,
      parameter_overrides: node.parameter_overrides ?? null,
      hasCapabilityScope: doesNodeHaveCapabilityScope(node),
      connector_binding: node.connector_binding ?? null,
      router_config: node.router_config ?? null,
    },
  }
}

/** Map a single wire edge to its canvas projection. */
export function projectGraphEdge(
  edge: PipelineGraphEdgeWire,
  _index: number
): CanvasEdgeProjection {
  const edgeType = edge.edge_type as 'normal' | 'reject' | 'conditional' | 'loop'
  return {
    id: edge.id ?? `edge-${_index}`,
    source: edge.source_node_id,
    target: edge.target_node_id,
    edgeType,
    hitl_review_config: edge.hitl_review_config ?? null,
    conditionExpression: edge.condition_expression ?? null,
    sourcePort: edge.source_port ?? 'out',
    targetPort: edge.target_port ?? 'in',
  }
}

/** Project a full graph response (nodes + edges) to the canvas types. */
export function projectGraph(
  graph: PipelineGraphResponseWire | PipelineGraphUpdateWire
): CanvasGraphProjection {
  const nodes = graph.nodes.map(projectGraphNode)
  const edges = graph.edges.map((edge, index) => projectGraphEdge(edge, index))
  return { nodes, edges }
}

/** ========================================================================
 *  Helpers (duplicated from PipelineEditorView to keep adapter self-contained)
 *  ========================================================================
 */

/** Short ID for fallback labels. */
function shortId(id: string): string {
  return id.slice(-4)
}

/** Whether a node declares a capability scope (displayed as a badge). */
function doesNodeHaveCapabilityScope(node: PipelineGraphNodeWire): boolean {
  const scope = node.capability_scope
  if (!scope) return false
  return (
    (scope.allowed_connectors?.length ?? 0) > 0 ||
    (scope.allowed_tools?.length ?? 0) > 0 ||
    (scope.context_scope?.length ?? 0) > 0
  )
}

/** ========================================================================
 *  UI state defaults / merge helpers
 *  ========================================================================
 */

/** Merge a canvas projection with UI state (editor-owned). */
export function withUiState(
  projection: CanvasNodeProjection,
  ui: Partial<CanvasNodeUiState> = {}
): CanvasNodeProjection & { ui: CanvasNodeUiState } {
  return {
    ...projection,
    ui: { ...defaultCanvasNodeUiState, ...ui },
  }
}

/** Merge edge projection with UI state. */
export function withEdgeUiState(
  projection: CanvasEdgeProjection,
  ui: Partial<CanvasEdgeUiState> = {}
): CanvasEdgeProjection & { ui: CanvasEdgeUiState } {
  // Edge style derived from edgeType
  const isLoop = projection.edgeType === 'loop'
  // Note: 'llm' is a node mode, not an edge type. Edge types are normal/reject/conditional/loop.
  const stroke = isLoop ? '#3b82f6' : '#64748b'
  const style: Record<string, string> = isLoop
    ? { stroke, strokeDasharray: '5,5' }
    : { stroke }

  return {
    ...projection,
    ui: {
      animated: isLoop,
      stroke,
      style,
      markerEnd: {
        type: 'ArrowClosed',
        width: 16,
        height: 16,
        color: stroke,
      },
      ...ui,
    },
  }
}
