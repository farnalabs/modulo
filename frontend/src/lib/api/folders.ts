import type { components } from './client'

/**
 * Shared shape for a folder row returned by the folder-list endpoints
 * (`/api/v1/pipeline-folders`, `/api/v1/schema-folders`, ...) and the
 * runtime validator every consumer runs before assigning the response.
 *
 * Derived from the generated contract rather than redeclared by hand: the
 * folder-list endpoints are typed `FolderResponse[]`, so this aliases the
 * fields the render path actually dereferences (`id`, `name`, `parent_id`).
 * Declaring the full wire shape by hand duplicated the generated contract and
 * went stale — the previous hand-written interface promised `sort_order:
 * number` that the predicate never validated.
 *
 * Why a validator and not just the type: `useApi().get<T>()` is an unchecked
 * compile-time cast — the runtime value is whatever the server sent, and a 204
 * yields `undefined as T`. The folder consumers dereference the value on every
 * render (`foldersList.length`, spread iteration, `f.parent_id`, `f.name`), so
 * a wrong shape reaches Vue's render path and crashes the whole view.
 */
export type FolderItem = Pick<components['schemas']['FolderResponse'], 'id' | 'name' | 'parent_id'>

/**
 * True only when `value` is an array whose every element is a folder the
 * render path can safely dereference: a non-null object with a string `id`,
 * a string `name`, and a `parent_id` that is a string or null.
 *
 * Deliberately rejects `undefined`/`null` (the useApi 204 empty-body case) and
 * any non-array container, and validates elements rather than only the
 * container — `Array.isArray([null])` is true, but `[null]` still throws when
 * the tree groups/sorts it. It also rejects SPARSE arrays: see the index loop
 * below.
 */
export function isFolderList(value: unknown): value is FolderItem[] {
  if (!Array.isArray(value)) return false
  // Iterate with `for...of`, not `every`: Array.prototype.every SKIPS holes, so
  // a container-only `every` check returned true for `isFolderList(new Array(2))`
  // and every consumer then crashed reading each hole as a folder row. The
  // array iterator `for...of` uses yields a hole as `undefined`, and
  // `isFolderItem` rejects it, so sparse arrays are rejected too.
  for (const item of value) {
    if (!isFolderItem(item)) return false
  }
  return true
}

function isFolderItem(value: unknown): value is FolderItem {
  if (typeof value !== 'object' || value === null) return false
  const item = value as Record<string, unknown>
  if (typeof item.id !== 'string') return false
  if (typeof item.name !== 'string') return false
  // `parent_id` is REQUIRED, not optional. Evidence (2026-10-15):
  //   - backend `FolderResponse` declares `parent_id: uuid.UUID | None` with no
  //     default, so Pydantic always serialises it (as `null` when unset);
  //   - no folder route sets `response_model_exclude_unset`;
  //   - the generated contract types it as a required `string | null`
  //     (schema.ts FolderResponse.parent_id).
  // So an element without `parent_id` violates the declared contract; accepting
  // it would hand consumers a value typed `string | null` that is actually
  // `undefined`, which is exactly the unsoundness this validator exists to
  // prevent.
  if (item.parent_id !== null && typeof item.parent_id !== 'string') return false
  return true
}

/**
 * Build a single, accurate log message for a rejected folder-list response.
 *
 * Kept in one place because the previous per-consumer message was factually
 * wrong for the case it was added to catch: for `[null]` the container WAS an
 * array and the failure was in an ELEMENT, yet it logged "received object" and
 * never mentioned the element. It also must not log the response body — a
 * tenancy review deliberately removed payload logging — so `response` is used
 * only to classify the container's shape.
 */
export function invalidFolderListMessage(endpoint: string, response: unknown): string {
  if (Array.isArray(response)) {
    return `[folders] Invalid folder list response from ${endpoint}: expected an array of folder objects, received an array containing a non-folder element`
  }
  const received = response === null ? 'null' : typeof response
  return `[folders] Invalid folder list response from ${endpoint}: expected an array of folder objects, received ${received}`
}
