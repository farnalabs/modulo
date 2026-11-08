// Canonical pipeline graph contract (FAR-2457).
// This dependency-free TypeScript schema intentionally mirrors the backend's
// strict graph payload: parse() throws and safeParse() reports unknown fields.

export interface GraphPosition { x: number; y: number }
export interface CanonicalPipelineGraphNode { id: string; node_type?: string; position: GraphPosition; idempotent?: boolean; join_partial_policy?: string; [key: string]: unknown }
export interface CanonicalPipelineGraphEdge { id: string; source_node_id: string; target_node_id: string; edge_type: string; source_port?: string; target_port?: string; [key: string]: unknown }
export interface CanonicalPipelineGraphUpdate { nodes: CanonicalPipelineGraphNode[]; edges: CanonicalPipelineGraphEdge[] }

const NODE_FIELDS = new Set([
  'id', 'node_type', 'agent_id', 'position', 'connector_binding', 'output_schema_id', 'input_schema_pin',
  'output_schema_pin', 'capability_scope', 'label', 'role', 'autonomy_recommendation', 'idempotent',
  'composite_ref', 'composite_parameter_values', 'composite_input_mapping', 'composite_output_mapping',
  'parameter_schema_id', 'parameter_set_id', 'parameter_overrides', 'template_id', 'mode', 'agent_commands',
  'commands_concatenation_string', 'agent_prompt', 'script_command', 'model_class', 'egress_policy',
  'egress_allowlist', 'resource_limits', 'read_only', 'git_credentials', 'wallclock_budget_seconds',
  'delivery_sentinel', 'single_pr_per_run', 'env_vars', 'context_files', 'timeout_seconds',
  'output_schema_json', 'schema_profile', 'description', 'stall_timeout_seconds', 'enable_heartbeat',
  'watch_log_path', 'stdout_percentage_delta', 'watch_globs', 'router_config', 'hitl_config', 'policy_gate',
  'fan_out', 'collect', 'aggregate', 'join_partial_policy', 'inputs', 'outputs', 'stdout_retention_mode',
  'stdout_max_bytes', 'workspace_inputs', 'await_completion', 'wait_timeout',
  'routing_mode', 'default_target', 'execution_metadata', 'max_input_length', 'token_budget',
  // Legacy execution fields still read by the backend compiler from imported or
  // historical snapshots; kept in sync with the backend node allowlist.
  'model_backend_id', 'prompt_template', 'routing_prompt', 'manual_prompt', 'allowed_hosts',
  'input_schema_json', 'output_schema_version', 'loop_intercept',
])
const EDGE_FIELDS = new Set([
  'id', 'source_node_id', 'target_node_id', 'edge_type', 'hitl_review_config', 'condition_expression',
  'source_port', 'target_port', 'default_target', 'max_iterations', 'routing_label',
  'source', 'target', 'type',
])
const NODE_TYPES = new Set(['agent', 'manual', 'composite', 'sandbox_agent', 'router', 'hitl', 'join', 'dispatch', 'connector'])
const EDGE_TYPES = new Set(['normal', 'reject', 'conditional', 'loop', 'llm'])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
function isIdentifier(value: unknown): value is string {
  // The backend's persisted graph contract treats IDs as opaque strings here.
  // UUID validation belongs to the API model; the editor also handles imported
  // and test graphs whose stable identifiers are not UUIDs.
  return typeof value === 'string' && value.length > 0
}
function applyDefaults(graph: CanonicalPipelineGraphUpdate): CanonicalPipelineGraphUpdate {
  graph.nodes = graph.nodes.map((node) => ({ idempotent: true, join_partial_policy: 'collect_and_proceed', ...node }))
  graph.edges = graph.edges.map((edge) => ({ source_port: 'out', target_port: 'in', ...edge }))
  return graph
}

function assertGraph(graph: unknown): asserts graph is CanonicalPipelineGraphUpdate {
  if (!isRecord(graph)) throw new Error('Graph must be an object')
  const unknownTop = Object.keys(graph).filter((key) => key !== 'nodes' && key !== 'edges')
  if (unknownTop.length > 0) throw new Error(`Graph contains unknown fields: ${unknownTop.join(', ')}`)
  if (!Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) throw new Error('Graph must contain nodes and edges arrays')
  graph.nodes.forEach((node, index) => {
    if (!isRecord(node)) throw new Error(`Node ${index} must be an object`)
    const unknown = Object.keys(node).filter((key) => !NODE_FIELDS.has(key))
    if (unknown.length > 0) throw new Error(`Node ${String(node.id ?? index)} contains unknown fields: ${unknown.join(', ')}`)
    if (!isIdentifier(node.id)) throw new Error(`Node ${index} has an invalid id`)
    if (!isRecord(node.position) || typeof node.position.x !== 'number' || typeof node.position.y !== 'number') throw new Error(`Node ${node.id} has an invalid position`)
    if (node.node_type !== undefined && (typeof node.node_type !== 'string' || !NODE_TYPES.has(node.node_type))) throw new Error(`Node ${node.id} has an invalid node_type`)
  })
  graph.edges.forEach((edge, index) => {
    if (!isRecord(edge)) throw new Error(`Edge ${index} must be an object`)
    const unknown = Object.keys(edge).filter((key) => !EDGE_FIELDS.has(key))
    if (unknown.length > 0) throw new Error(`Edge ${String(edge.id ?? index)} contains unknown fields: ${unknown.join(', ')}`)
    if (!isIdentifier(edge.id) || !isIdentifier(edge.source_node_id) || !isIdentifier(edge.target_node_id)) throw new Error(`Edge ${index} has invalid ids`)
    if (typeof edge.edge_type !== 'string' || !EDGE_TYPES.has(edge.edge_type)) throw new Error(`Edge ${edge.id} has an invalid edge_type`)
  })
}

export const CanonicalPipelineGraphNodeSchema = { parse(value: unknown): CanonicalPipelineGraphNode { const graph = { nodes: [value], edges: [] } as CanonicalPipelineGraphUpdate; assertGraph(graph); return applyDefaults(graph).nodes[0] } }
export const CanonicalPipelineGraphEdgeSchema = { parse(value: unknown): CanonicalPipelineGraphEdge { const graph = { nodes: [], edges: [value] } as CanonicalPipelineGraphUpdate; assertGraph(graph); return applyDefaults(graph).edges[0] } }
export const CanonicalPipelineGraphUpdateSchema = {
  parse(value: unknown): CanonicalPipelineGraphUpdate { assertGraph(value); return applyDefaults(value) },
  safeParse(value: unknown): { success: true; data: CanonicalPipelineGraphUpdate } | { success: false; error: Error } {
    try { assertGraph(value); return { success: true, data: applyDefaults(value) } } catch (error) { return { success: false, error: error instanceof Error ? error : new Error(String(error)) } }
  },
}

export function validateCanonicalGraph(graph: unknown): CanonicalPipelineGraphUpdate { return CanonicalPipelineGraphUpdateSchema.parse(graph) }
export function safeValidateCanonicalGraph(graph: unknown) { return CanonicalPipelineGraphUpdateSchema.safeParse(graph) }
