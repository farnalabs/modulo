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

import { MarkerType } from '@vue-flow/core'
import type { components } from '@/lib/api/schema'
import { NODE_REGISTRY, getNodeRegistryEntry, type NodeTypeId } from '@/components/pipeline/nodes/NodeRegistry'
import { CANVAS_EDGE_STROKE } from '@/constants/canvas'
import { shortId } from '@/utils/format'
import { doesNodeHaveCapabilityScope } from '@/utils/pipeline-graph'

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

/** Canvas node types that exist as distinct visual cards.
 *
 * This is the editor's node-type registry vocabulary, not a separate mapping:
 * ``getNodeRegistryEntry`` already falls back to ``agent`` for unknown types.
 */
export type CanvasNodeType = NodeTypeId

/** Map a backend ``node_type`` to its canvas card type.
 *
 * Delegates to the editor's single node registry so the adapter cannot drift
 * from the types the editor actually renders (and the ``agent`` fallback lives
 * in exactly one place).
 *
 * The registry is a plain object, so a wire value that names an inherited
 * ``Object.prototype`` member (``toString``, ``constructor``, ...) would
 * otherwise resolve to that member instead of falling back. The own-property
 * check keeps such a value on the ``agent`` card, where every unknown type
 * belongs.
 *
 * Production consumer: ``PipelineEditorView.vue`` ``convertBackendNode`` (the
 * graph-load path for both the live graph and a viewed snapshot).
 */
export function getCanvasType(backendType: string): CanvasNodeType {
  if (!Object.prototype.hasOwnProperty.call(NODE_REGISTRY, backendType)) {
    return 'agent'
  }
  return getNodeRegistryEntry(backendType).id as CanvasNodeType
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

/** Every edge variant the editor renders. ``llm`` is a real wire value the
 *  edge editor offers alongside the four routing types. */
export type CanvasEdgeType = 'normal' | 'reject' | 'conditional' | 'loop' | 'llm'

/** The canonical list of canvas edge variants, used by the exhaustiveness
 *  regression test. Kept in lockstep with {@link CanvasEdgeType} by the
 *  compile-time ``Record<CanvasEdgeType, ...>`` style table below. */
export const CANVAS_EDGE_TYPES = [
  'normal',
  'reject',
  'conditional',
  'loop',
  'llm',
] as const satisfies readonly CanvasEdgeType[]

/** Wire fields the edge form reads that the generated OpenAPI edge schema does
 *  not (yet) declare. They are part of the live graph payload the editor has
 *  always consumed, so the projection carries them verbatim rather than
 *  inventing a backend contract. */
export interface PipelineGraphEdgeExtras {
  max_iterations?: number | null
  routing_label?: string | null
}

/** The wire edge as the editor actually receives it. */
export type PipelineGraphEdgeInput = PipelineGraphEdgeWire & PipelineGraphEdgeExtras

/** The semantic stripe the canvas edge component (``EdgeBadges``) and the edge
 *  inspector consume. Mirrors the historical ``data`` wrapper exactly. */
export interface CanvasEdgeData {
  /** HITL review config on the edge. */
  hitl_review_config: PipelineGraphEdgeWire['hitl_review_config'] | null
  /** Raw wire ``edge_type``, defaulted to ``normal`` for display. */
  edge_type: string
  /** Conditional edge expression. */
  condition_expression: string | null
  /** Loop iteration cap (0 when unset/non-loop). */
  max_iterations: number
  /** LLM routing label ('' when unset/non-llm). */
  routing_label: string
}

/** Edge projection: wire payload -> semantic + geometry + the canvas data
 *  wrapper the editor reads. */
export interface CanvasEdgeProjection {
  id: string
  source: string
  target: string
  /** Known semantic edge variant, used to derive UI styling. */
  edgeType: CanvasEdgeType
  /** HITL review config on the edge. */
  hitl_review_config: PipelineGraphEdgeWire['hitl_review_config'] | null
  /** Conditional edge expression. */
  conditionExpression: string | null
  /** Source port. */
  sourcePort: string
  /** Target port. */
  targetPort: string
  /** The semantic data wrapper the canvas edge + inspector consume. */
  data: CanvasEdgeData
}

/** UI-only edge styling (animated, stroke, marker). NEVER from wire. */
export interface CanvasEdgeUiState {
  animated: boolean
  stroke: string
  style: Record<string, string>
  markerEnd: {
    type: MarkerType
    width: number
    height: number
    color: string
  }
}

/** A Vue Flow edge as the editor renders it: geometry, UI styling and the
 *  semantic ``data`` wrapper, all derived from the wire payload. */
export interface CanvasFlowEdge {
  id: string
  source: string
  target: string
  type: 'smoothstep'
  animated: boolean
  style: Record<string, string>
  markerEnd: CanvasEdgeUiState['markerEnd']
  data: CanvasEdgeData
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

/** Map a single wire node to its canvas projection.
 *
 * Production consumer: ``PipelineEditorView.vue`` ``convertBackendNode``, which
 * both ``loadGraph()`` (live graph) and ``loadViewedSnapshot()`` map every node
 * through. The editor normalises the three untyped fields it feeds in
 * (``position``, ``label``, ``description``) to the same fallbacks it always
 * used, then renders this projection directly.
 */
export function projectGraphNode(node: PipelineGraphNodeWire): CanvasNodeProjection {
  const nodeType = getCanvasType(node.node_type)

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

/** Normalise a wire ``edge_type`` onto the canvas variant union.
 *
 * Unknown values fall back to ``normal`` for styling, which is exactly how the
 * editor has always styled them (only ``loop`` and ``llm`` get their own
 * treatment). The raw value is still preserved verbatim in
 * {@link CanvasEdgeData.edge_type} for the edge form.
 */
export function normalizeCanvasEdgeType(raw: string | null | undefined): CanvasEdgeType {
  return (CANVAS_EDGE_TYPES as readonly string[]).includes(raw ?? '')
    ? (raw as CanvasEdgeType)
    : 'normal'
}

/** Map a single wire edge to its canvas projection.
 *
 * Production consumer: ``PipelineEditorView.vue`` ``convertBackendEdge`` -
 * through {@link projectCanvasEdge} - which both ``loadGraph()`` (live graph)
 * and ``loadViewedSnapshot()`` map every edge through. This is the ONLY
 * backend-edge-to-canvas mapping; the view must not re-derive any edge field.
 */
export function projectGraphEdge(
  edge: PipelineGraphEdgeInput,
  index: number
): CanvasEdgeProjection {
  // Defaults mirror the editor's long-standing inline mapping exactly: the raw
  // edge type is passed through (defaulting to `normal`), and the two
  // form-only extras default to their empty values.
  const wireEdgeType = edge.edge_type || 'normal'
  const hitlReviewConfig = edge.hitl_review_config || null
  const conditionExpression = edge.condition_expression || null
  const maxIterations = edge.max_iterations || 0
  const routingLabel = edge.routing_label || ''
  return {
    id: edge.id || `edge-${index}`,
    source: edge.source_node_id,
    target: edge.target_node_id,
    edgeType: normalizeCanvasEdgeType(wireEdgeType),
    hitl_review_config: hitlReviewConfig,
    conditionExpression,
    sourcePort: edge.source_port ?? 'out',
    targetPort: edge.target_port ?? 'in',
    data: {
      hitl_review_config: hitlReviewConfig,
      edge_type: wireEdgeType,
      condition_expression: conditionExpression,
      max_iterations: maxIterations,
      routing_label: routingLabel,
    },
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

/** Exhaustive UI style table for every canvas edge variant.
 *
 * ``Record<CanvasEdgeType, ...>`` makes adding a variant to the union a type
 * error until it is styled here, so a projection change cannot silently drop
 * one. Strokes mirror the editor's pre-adapter inline mapping exactly: the
 * shared muted-foreground token for the routing types, blue dashed for loop,
 * violet for llm routing. */
const EDGE_UI_STYLES: Record<
  CanvasEdgeType,
  { stroke: string; animated: boolean; strokeDasharray?: string }
> = {
  normal: { stroke: CANVAS_EDGE_STROKE, animated: false },
  reject: { stroke: CANVAS_EDGE_STROKE, animated: false },
  conditional: { stroke: CANVAS_EDGE_STROKE, animated: false },
  loop: { stroke: '#3b82f6', animated: true, strokeDasharray: '5,5' },
  llm: { stroke: '#8b5cf6', animated: false },
}

/** Merge edge projection with UI state. This is the single place UI-only
 *  styling is layered onto an edge - callers never derive strokes themselves. */
export function withEdgeUiState(
  projection: CanvasEdgeProjection,
  ui: Partial<CanvasEdgeUiState> = {}
): CanvasEdgeProjection & { ui: CanvasEdgeUiState } {
  const { stroke, animated, strokeDasharray } = EDGE_UI_STYLES[projection.edgeType]
  const style: Record<string, string> = strokeDasharray
    ? { stroke, strokeDasharray }
    : { stroke }

  return {
    ...projection,
    ui: {
      animated,
      stroke,
      style,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 16,
        height: 16,
        color: stroke,
      },
      ...ui,
    },
  }
}

/** Map a wire edge to the Vue Flow edge the editor renders.
 *
 * This composes the semantic projection with the UI styling layer, so it is
 * the adapter's single backend-edge-to-canvas entry point. The editor's
 * ``convertBackendEdge`` is a thin delegate. */
export function projectCanvasEdge(edge: PipelineGraphEdgeInput, index: number): CanvasFlowEdge {
  const projected = withEdgeUiState(projectGraphEdge(edge, index))
  return {
    id: projected.id,
    source: projected.source,
    target: projected.target,
    type: 'smoothstep',
    animated: projected.ui.animated,
    style: projected.ui.style,
    markerEnd: projected.ui.markerEnd,
    data: projected.data,
  }
}
