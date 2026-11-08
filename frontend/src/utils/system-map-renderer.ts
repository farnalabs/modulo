import type { NodeType, RelationshipType, SystemMapNode, SystemMapRelationship } from '../stores/systemMaps'
import type { SystemMapRendererModel, SystemMapRendererOptions } from '../types/system-map-renderer'

export function createSystemMapRendererModel(
  nodes: SystemMapNode[],
  relationships: SystemMapRelationship[],
  options: SystemMapRendererOptions = {},
): SystemMapRendererModel {
  const readOnly = options.readOnly === true
  const rendererNodes = nodes.map((node) => ({
    id: node.id,
    nodeKey: node.node_key,
    type: node.node_type as NodeType,
    label: node.display_name,
    x: Number(node.position_x),
    y: Number(node.position_y),
    selected: node.node_key === options.selectedNodeKey,
    readOnly,
  }))
  const nodeKeys = new Set(rendererNodes.map((node) => node.nodeKey))
  const edges = relationships
    .filter((relationship) => nodeKeys.has(relationship.source_node_key) && nodeKeys.has(relationship.target_node_key))
    .map((relationship) => ({
      id: relationship.id,
      source: relationship.source_node_key,
      target: relationship.target_node_key,
      type: relationship.relationship_type as RelationshipType,
    }))

  return { nodes: rendererNodes, edges, readOnly }
}
