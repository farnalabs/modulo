import type {
  DiscoveryReport,
  DiscoverySelection,
  SystemMapNode,
  SystemMapRelationship,
} from '../../stores/systemMaps'

const COLUMNS = 3
const COLUMN_WIDTH = 220
const ROW_HEIGHT = 140
const ORIGIN = 40

function gridPosition(index: number): { x: string; y: string } {
  return {
    x: String(ORIGIN + (index % COLUMNS) * COLUMN_WIDTH),
    y: String(ORIGIN + Math.floor(index / COLUMNS) * ROW_HEIGHT),
  }
}

/**
 * Apply the operator's accept/reject decisions to the current draft graph.
 * Only accepted inferred nodes and edges are merged; already-present keys are
 * never duplicated. Pure so the authoring behaviour is unit-testable without a
 * mounted view.
 */
export function mergeDiscoverySelections(
  nodes: SystemMapNode[],
  relationships: SystemMapRelationship[],
  discovery: DiscoveryReport | null,
  selections: DiscoverySelection,
): { nodes: SystemMapNode[]; relationships: SystemMapRelationship[] } {
  // The discovery endpoint persists a draft containing every inferred item, so
  // a rejection must actively remove it rather than simply not adding it.
  const rejectedNodeKeys = new Set(
    selections.rejectedNodes
      .map((index) => discovery?.inferred_nodes?.[index]?.node_key)
      .filter((key): key is string | number => key !== undefined)
      .map(String),
  )
  const rejectedEdgeKeys = new Set(
    selections.rejectedEdges.flatMap((index) => {
      const edge = discovery?.inferred_edges?.[index]
      if (!edge) return []
      return [`${String(edge.source_node_key)}->${String(edge.target_node_key)}:${String(edge.relationship_type)}`]
    }),
  )

  const keptNodes = nodes.filter((node) => !rejectedNodeKeys.has(node.node_key))
  const existingNodeKeys = new Set(keptNodes.map((node) => node.node_key))
  const startIndex = keptNodes.length
  const addedNodes: SystemMapNode[] = []
  selections.acceptedNodes.forEach((index, offset) => {
    const inferred = discovery?.inferred_nodes?.[index]
    if (!inferred) return
    const key = String(inferred.node_key)
    if (existingNodeKeys.has(key)) return
    existingNodeKeys.add(key)
    const position = gridPosition(startIndex + offset)
    addedNodes.push({
      id: `discovered-${key}`,
      organisation_id: '',
      revision_id: '',
      node_key: key,
      node_type: String(inferred.node_type),
      display_name: String(inferred.display_name),
      config_json: (inferred.config_json as Record<string, unknown>) ?? {},
      pipeline_id: (inferred.pipeline_id as string | null) ?? null,
      connector_instance_id: (inferred.connector_instance_id as string | null) ?? null,
      owner_team_id: (inferred.owner_team_id as string | null) ?? null,
      eval_id: null,
      position_x: position.x,
      position_y: position.y,
      deleted_at: null,
      deleted_by: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    } as SystemMapNode)
  })

  const keptRelationships = relationships.filter((relationship) => {
    const edgeKey = `${relationship.source_node_key}->${relationship.target_node_key}:${relationship.relationship_type}`
    return !rejectedEdgeKeys.has(edgeKey)
  })
  const existingEdgeKeys = new Set(keptRelationships.map((relationship) => `${relationship.source_node_key}->${relationship.target_node_key}:${relationship.relationship_type}`))
  const addedRelationships: SystemMapRelationship[] = []
  for (const index of selections.acceptedEdges) {
    const edge = discovery?.inferred_edges?.[index]
    if (!edge) continue
    const source = String(edge.source_node_key)
    const target = String(edge.target_node_key)
    const type = String(edge.relationship_type)
    const edgeKey = `${source}->${target}:${type}`
    if (existingEdgeKeys.has(edgeKey)) continue
    existingEdgeKeys.add(edgeKey)
    addedRelationships.push({
      id: `discovered-rel-${index}-${source}-${target}`,
      organisation_id: '',
      revision_id: '',
      source_node_key: source,
      target_node_key: target,
      relationship_type: type,
      config_json: (edge.config_json as Record<string, unknown>) ?? {},
      deleted_at: null,
      deleted_by: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    } as SystemMapRelationship)
  }

  const mergedNodes = [...keptNodes, ...addedNodes]
  const mergedNodeKeys = new Set(mergedNodes.map((node) => node.node_key))
  // Drop any relationship whose endpoint was removed (rejected or never accepted).
  const mergedRelationships = [...keptRelationships, ...addedRelationships].filter(
    (relationship) => mergedNodeKeys.has(relationship.source_node_key) && mergedNodeKeys.has(relationship.target_node_key),
  )
  return { nodes: mergedNodes, relationships: mergedRelationships }
}
