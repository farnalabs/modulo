import { readonly } from 'vue'
import { useStorage } from '@vueuse/core'

const viewMode = useStorage<'essentials' | 'advanced'>('sidebar-view-mode', 'essentials')
const groupPrefs = useStorage<Record<string, boolean>>('sidebar-group-prefs', {})

function toggleGroup(id: string, defaultCollapsed: boolean) {
  groupPrefs.value[id] = !isGroupCollapsed(id, defaultCollapsed)
}

function isGroupCollapsed(id: string, defaultCollapsed: boolean): boolean {
  return groupPrefs.value[id] ?? defaultCollapsed
}

function setViewMode(mode: 'essentials' | 'advanced') {
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
