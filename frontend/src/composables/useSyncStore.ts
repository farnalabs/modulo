import { ref, triggerRef } from 'vue'
import type { EventBusEvent } from '@/types/events'

export interface SyncableStore {
  dirtyIds: Set<string>
  fetch(id: string): Promise<void>
  remove(id: string): void
}

export function createSyncAdapter(store: SyncableStore) {
  return function handleSyncEvent(event: EventBusEvent): void {
    if (event.action === 'deleted') {
      try {
        store.remove(event.id)
      } catch (e) {
        console.error('[SyncAdapter] remove error', e)
      }
    } else {
      store.fetch(event.id).catch((err: unknown) => {
        console.error('[SyncAdapter] fetch error', err)
      })
    }
  }
}

export function useDirtyTracker() {
  const dirtyIds = ref(new Set<string>())

  if (import.meta.hot) {
    import.meta.hot.dispose(() => {
      dirtyIds.value.clear()
    })
  }

  function markDirty(id: string): void {
    dirtyIds.value.add(id)
    triggerRef(dirtyIds)
  }

  function markClean(id: string): void {
    dirtyIds.value.delete(id)
    triggerRef(dirtyIds)
  }

  function isDirty(id: string): boolean {
    return dirtyIds.value.has(id)
  }

  return { dirtyIds, markDirty, markClean, isDirty }
}
