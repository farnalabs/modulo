import { describe, it, expect } from 'vitest'
import { isFolderList } from '../lib/api/folders'

// One canonical valid row; the consumer render path dereferences id, name and
// parent_id (the folder tree groups by parent_id and sorts by name).
const validFolder = { id: 'f1', organisation_id: 'org-1', name: 'Root', parent_id: null, sort_order: 0 }

describe('isFolderList', () => {
  it('accepts an empty array', () => {
    expect(isFolderList([])).toBe(true)
  })

  it('accepts a valid single-element array', () => {
    expect(isFolderList([validFolder])).toBe(true)
  })

  it('accepts a valid nested array', () => {
    expect(isFolderList([validFolder, { ...validFolder, id: 'f2', name: 'Child', parent_id: 'f1' }])).toBe(true)
  })

  it('accepts an element whose parent_id is undefined', () => {
    expect(isFolderList([{ id: 'f1', name: 'Root' }])).toBe(true)
  })

  it('rejects undefined (the `undefined as T` empty-body case)', () => {
    expect(isFolderList(undefined)).toBe(false)
  })

  it('rejects null', () => {
    expect(isFolderList(null)).toBe(false)
  })

  it('rejects an empty object', () => {
    expect(isFolderList({})).toBe(false)
  })

  it('rejects a paginated envelope object', () => {
    expect(isFolderList({ items: [] })).toBe(false)
  })

  it('rejects a string', () => {
    expect(isFolderList('x')).toBe(false)
  })

  it('rejects a number', () => {
    expect(isFolderList(42)).toBe(false)
  })

  it('rejects an array containing null', () => {
    expect(isFolderList([null])).toBe(false)
  })

  it('rejects an array containing an empty object', () => {
    expect(isFolderList([{}])).toBe(false)
  })

  it('rejects an element with a non-string id', () => {
    expect(isFolderList([{ id: 1, name: 'a' }])).toBe(false)
  })

  it('rejects an element with a non-string name', () => {
    expect(isFolderList([{ id: 'f1', name: 1 }])).toBe(false)
  })

  it('rejects an element with a non-string, non-null parent_id', () => {
    expect(isFolderList([{ id: 'f1', name: 'a', parent_id: 42 }])).toBe(false)
  })
})
