import { describe, it, expect } from 'vitest'
import { layoutNodes } from '../utils/graph-layout'

describe('layoutNodes', () => {
  it('lays out a single node at the origin', () => {
    const result = layoutNodes([{ id: 'a' }], [])
    expect(result).toEqual([
      { id: 'a', type: 'agent', position: { x: 0, y: 0 }, data: { label: 'a' } },
    ])
  })

  it('places independent nodes on one row centered around x=0', () => {
    const result = layoutNodes([{ id: 'a' }, { id: 'b' }], [])
    expect(result.map(n => n.position.y)).toEqual([0, 0])
    expect(result.map(n => n.position.x)).toEqual([-120, 120])
  })

  it('places chained nodes on successive rows', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
      [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }],
    )
    expect(result).toEqual([
      { id: 'a', type: 'agent', position: { x: 0, y: 0 }, data: { label: 'a' } },
      { id: 'b', type: 'agent', position: { x: -120, y: 160 }, data: { label: 'b' } },
      { id: 'c', type: 'agent', position: { x: 120, y: 160 }, data: { label: 'c' } },
    ])
  })

  it('places a diamond graph with branching nodes on separate rows', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }],
      [
        { source: 'a', target: 'b' },
        { source: 'a', target: 'c' },
        { source: 'b', target: 'd' },
        { source: 'c', target: 'd' },
      ],
    )
    const byId = Object.fromEntries(result.map(n => [n.id, n]))
    expect(byId.a.position.y).toBe(0)
    expect(byId.b.position.y).toBe(160)
    expect(byId.c.position.y).toBe(160)
    expect(byId.d.position.y).toBe(160)
  })

  it('uses node_type and label from the input when provided', () => {
    const result = layoutNodes(
      [{ id: 'a', node_type: 'schema', label: 'Schema A' }],
      [],
    )
    expect(result[0]).toMatchObject({ type: 'schema', data: { label: 'Schema A' } })
  })

  it('handles cyclic graphs without infinite looping', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }],
      [{ source: 'a', target: 'b' }, { source: 'b', target: 'a' }],
    )
    expect(result).toHaveLength(2)
  })

  it('respects custom layout options', () => {
    const result = layoutNodes(
      [{ id: 'a' }, { id: 'b' }],
      [],
      { nodeWidth: 100, nodeHeight: 50, xPad: 20, yPad: 30 },
    )
    expect(result[1].position).toEqual({ x: 60, y: 0 })
  })
})
