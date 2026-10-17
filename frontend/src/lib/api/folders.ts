/**
 * Shared shape for a folder row returned by the folder-list endpoints
 * (`/api/v1/pipeline-folders`, `/api/v1/schema-folders`, ...) and the
 * runtime validator every consumer runs before assigning the response.
 *
 * Why a validator and not just the type: `useApi().get<T>()` is an unchecked
 * compile-time cast — the runtime value is whatever the server sent, and a 204
 * yields `undefined as T`. The folder consumers dereference the value on every
 * render (`foldersList.length`, spread iteration, `f.parent_id`, `f.name`), so
 * a wrong shape reaches Vue's render path and crashes the whole view.
 */
export interface FolderItem {
  id: string
  organisation_id: string
  name: string
  parent_id: string | null
  sort_order: number
}

/**
 * True only when `value` is an array whose every element is a folder the
 * render path can safely dereference: a non-null object with a string `id`,
 * a string `name`, and a `parent_id` that is a string, null or undefined.
 *
 * Deliberately rejects `undefined`/`null` (the useApi 204 empty-body case) and
 * any non-array container, and validates elements rather than only the
 * container — `Array.isArray([null])` is true, but `[null]` still throws when
 * the tree groups/sorts it.
 */
export function isFolderList(value: unknown): value is FolderItem[] {
  if (!Array.isArray(value)) return false
  return value.every(isFolderItem)
}

function isFolderItem(value: unknown): value is FolderItem {
  if (typeof value !== 'object' || value === null) return false
  const item = value as Record<string, unknown>
  if (typeof item.id !== 'string') return false
  if (typeof item.name !== 'string') return false
  const parentId = item.parent_id
  if (parentId !== null && parentId !== undefined && typeof parentId !== 'string') return false
  return true
}
