import { ref } from 'vue'
import type { EventBusEvent } from '@/types/events'

export interface SyncableStore {
  dirtyIds: Set<string>
  fetch(id: string): Promise<void>
  remove(id: string): void
}

export function createSyncAdapter(store: SyncableStore) {
  return function handleSyncEvent(event: EventBusEvent): void {
    if (store.dirtyIds.has(event.id)) return
    if (event.action === 'deleted') {
      store.remove(event.id)
    } else {
      store.fetch(event.id)
    }
  }
}

export function useDirtyTracker() {
  const dirtyIds = ref(new Set<string>())

  function markDirty(id: string): void {
    dirtyIds.value.add(id)
  }

  function markClean(id: string): void {
    dirtyIds.value.delete(id)
  }

  function isDirty(id: string): boolean {
    return dirtyIds.value.has(id)
  }

  return { dirtyIds, markDirty, markClean, isDirty }
}
