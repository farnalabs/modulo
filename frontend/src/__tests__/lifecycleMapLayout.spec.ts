import { describe, it, expect } from 'vitest'
import { formatRefLabel, computeLifecycleMapLayout } from '../stores/lifecycleMaps'

describe('formatRefLabel', () => {
  it('uppercases the kind and keeps the ref', () => {
    expect(formatRefLabel('pr', '2391')).toBe('PR 2391')
  })

  it('uppercases a mixed-case kind', () => {
    expect(formatRefLabel('iSsUe', 'PROJ/42')).toBe('ISSUE PROJ/42')
  })

  it('returns the ref alone when kind is empty', () => {
    expect(formatRefLabel('', '2391')).toBe('2391')
  })
})

describe('computeLifecycleMapLayout', () => {
  it('layers a linear chain left-to-right', () => {
    const layout = computeLifecycleMapLayout(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
      [
        { source: 'a', target: 'b' },
        { source: 'b', target: 'c' },
      ],
    )
    expect(layout.a.x).toBeLessThan(layout.b.x)
    expect(layout.b.x).toBeLessThan(layout.c.x)
    expect(layout.a.y).toBe(layout.b.y)
    expect(layout.b.y).toBe(layout.c.y)
  })

  it('places a split source and its branches on the same column', () => {
    const layout = computeLifecycleMapLayout(
      [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
      [
        { source: 'a', target: 'b' },
        { source: 'a', target: 'c' },
      ],
    )
    expect(layout.b.x).toBe(layout.c.x)
    expect(layout.b.y).not.toBe(layout.c.y)
  })

  it('returns an empty map for no stages', () => {
    expect(computeLifecycleMapLayout([], [])).toEqual({})
  })
})
