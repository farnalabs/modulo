import { describe, it, expect } from 'vitest'
import { isFolderList, invalidFolderListMessage } from '../lib/api/folders'

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

  it('rejects an element whose parent_id is absent', () => {
    // The backend FolderResponse declares `parent_id: uuid.UUID | None` with no
    // default (so it is always serialised, as null when unset) and no folder
    // route sets response_model_exclude_unset; the generated contract types it
    // as a required `string | null`. Absence therefore violates the contract,
    // and accepting it would hand consumers a value typed `string | null` that
    // is actually undefined.
    expect(isFolderList([{ id: 'f1', name: 'Root' }])).toBe(false)
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

  it('rejects a sparse array (holes are not folder elements)', () => {
    // Array.prototype.every SKIPS holes, so a container-only `every` check
    // returned true for `new Array(2)` and every consumer then crashed reading
    // the holes as folder rows. The predicate must visit holes and reject them.
    expect(isFolderList(new Array(2))).toBe(false)
  })

  it('rejects an array containing null', () => {
    expect(isFolderList([null])).toBe(false)
  })

  it('rejects an array containing an empty object', () => {
    expect(isFolderList([{}])).toBe(false)
  })

  it('rejects an element with a non-string id', () => {
    // `parent_id` is supplied as a valid `null` so the id type check is the ONLY
    // failing field. Omitting it made the element fail the parent_id check
    // first, masking this case: the id check could be deleted and this test
    // would still pass.
    expect(isFolderList([{ id: 1, name: 'a', parent_id: null }])).toBe(false)
  })

  it('rejects an element with a non-string name', () => {
    // `parent_id` is supplied as a valid `null` so the name type check is the
    // ONLY failing field (see the non-string-id case above).
    expect(isFolderList([{ id: 'f1', name: 1, parent_id: null }])).toBe(false)
  })

  it('rejects an element with a non-string, non-null parent_id', () => {
    expect(isFolderList([{ id: 'f1', name: 'a', parent_id: 42 }])).toBe(false)
  })
})

describe('invalidFolderListMessage', () => {
  it('names the endpoint and reports a non-array container by its actual type', () => {
    const message = invalidFolderListMessage('/api/v1/pipeline-folders', { items: [] })
    expect(message).toContain('/api/v1/pipeline-folders')
    expect(message).toContain('received object')
    // A non-array container is NOT an element failure.
    expect(message).not.toContain('element')
  })

  it('reports null as null, not as an object', () => {
    const message = invalidFolderListMessage('/api/v1/pipeline-folders', null)
    expect(message).toContain('received null')
  })

  it('distinguishes an array with a bad element from a non-array container', () => {
    // For [null] the container WAS an array — the failure is in an element, and
    // the message must say so rather than the old "received object".
    const elementMessage = invalidFolderListMessage('/api/v1/pipeline-folders', [null])
    const containerMessage = invalidFolderListMessage('/api/v1/pipeline-folders', null)
    expect(elementMessage).toContain('/api/v1/pipeline-folders')
    expect(elementMessage).toContain('array containing a non-folder element')
    expect(elementMessage).not.toBe(containerMessage)
  })

  it('does not log the response body', () => {
    const message = invalidFolderListMessage('/api/v1/pipeline-folders', { secret: 'leaky-token' })
    expect(message).not.toContain('leaky-token')
  })
})
