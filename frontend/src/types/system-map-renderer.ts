import type { NodeType, RelationshipType, SystemMapNode, SystemMapRelationship } from '../stores/systemMaps'

export interface SystemMapRendererNode {
  id: string
  nodeKey: string
  type: NodeType
  label: string
  x: number
  y: number
  selected: boolean
  readOnly: boolean
}

export interface SystemMapRendererEdge {
  id: string
  source: string
  target: string
  type: RelationshipType
}

export interface SystemMapRendererModel {
  nodes: SystemMapRendererNode[]
  edges: SystemMapRendererEdge[]
  readOnly: boolean
}

export interface SystemMapRendererOptions {
  readOnly?: boolean
  selectedNodeKey?: string | null
}

export type SystemMapRendererInput = Pick<SystemMapNode, 'id' | 'node_key' | 'node_type' | 'display_name' | 'position_x' | 'position_y'>
export type SystemMapRendererRelationshipInput = Pick<SystemMapRelationship, 'id' | 'source_node_key' | 'target_node_key' | 'relationship_type'>
