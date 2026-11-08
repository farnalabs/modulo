import { describe, expect, it } from 'vitest'
import { NODE_REGISTRY, NODE_REGISTRY_ORDER, getNodeRegistryEntry } from '../components/pipeline/nodes/NodeRegistry'

describe('pipeline node registry', () => {
  it('contains every supported node type with the required metadata', () => {
    expect(NODE_REGISTRY_ORDER).toHaveLength(6)
    for (const nodeType of NODE_REGISTRY_ORDER) {
      const entry = NODE_REGISTRY[nodeType]
      expect(entry.id).toBe(nodeType)
      expect(entry.label).toBeTruthy()
      expect(entry.glyph).toBeTruthy()
      expect(entry.borderStyle).toBeTruthy()
      expect(entry.colour).toBeTruthy()
      expect(entry.icon).toBeTruthy()
      expect(typeof entry.hasConfigPanel).toBe('boolean')
    }
  })

  it('falls back to the agent entry for unknown types', () => {
    expect(getNodeRegistryEntry('not-real')).toBe(NODE_REGISTRY.agent)
  })
})
