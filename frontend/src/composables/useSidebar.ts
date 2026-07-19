import { readonly } from 'vue'
import { useStorage } from '@vueuse/core'

export type ViewMode = 'simple' | 'advanced'

const viewMode = useStorage<ViewMode>('sidebar-view-mode', 'simple')
const groupPrefs = useStorage<Record<string, boolean>>('sidebar-group-prefs', {})

function toggleGroup(id: string, defaultCollapsed: boolean) {
  groupPrefs.value[id] = !isGroupCollapsed(id, defaultCollapsed)
}

function isGroupCollapsed(id: string, defaultCollapsed: boolean): boolean {
  return groupPrefs.value[id] ?? defaultCollapsed
}

function setViewMode(mode: ViewMode) {
  viewMode.value = mode
}

export function useSidebar() {
  return {
    viewMode: readonly(viewMode),
    groupPrefs: readonly(groupPrefs),
    toggleGroup,
    isGroupCollapsed,
    setViewMode,
  }
}
