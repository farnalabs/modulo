import { computed, ref, type ComputedRef, type Ref } from 'vue'

export interface CursorPageStack {
  /** Cursor for the page currently displayed (null = first page). */
  cursor: Ref<string | null>
  /** Cursor the API returned for the following page, if any. */
  nextCursor: Ref<string | null>
  /** 1-based page number, derived from the depth of the back-stack. */
  page: Ref<number>
  /** True when there is an earlier page to return to. */
  canGoPrevious: ComputedRef<boolean>
  goToNextPage: () => void
  goToPreviousPage: () => void
  /** Return to the first page and drop the back-stack (e.g. on filter change). */
  reset: () => void
}

/**
 * Shared cursor-based pagination. The list endpoints return only
 * `next_cursor`, so the pager keeps a back-stack of the cursor used to load
 * each earlier page (null = first page) to make Previous work.
 *
 * `load` is the view's refetch function; it is invoked after the cursor moves.
 * Declare the composable before the view's data fetch: the fetcher reads
 * `cursor` synchronously on setup, and a later declaration hits the TDZ
 * ReferenceError documented in FAR-608 / FAR-2173.
 */
export function useCursorPageStack(load: () => unknown): CursorPageStack {
  const cursor = ref<string | null>(null)
  const nextCursor = ref<string | null>(null)
  const page = ref(1)
  const history = ref<(string | null)[]>([])

  const canGoPrevious = computed(() => history.value.length > 0)

  function goToNextPage() {
    const next = nextCursor.value
    if (!next) return
    history.value.push(cursor.value)
    page.value += 1
    cursor.value = next
    load()
  }

  function goToPreviousPage() {
    if (history.value.length === 0) return
    cursor.value = history.value.pop() ?? null
    page.value = Math.max(1, page.value - 1)
    load()
  }

  function reset() {
    cursor.value = null
    history.value = []
    page.value = 1
  }

  return {
    cursor,
    nextCursor,
    page,
    canGoPrevious,
    goToNextPage,
    goToPreviousPage,
    reset,
  }
}
