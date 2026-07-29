import { readonly } from 'vue'
import { useStorage } from '@vueuse/core'

export type ViewMode = 'simple' | 'advanced'

const groupPrefs = useStorage<Record<string, boolean>>('sidebar-group-prefs', {})

function toggleGroup(id: string, defaultCollapsed: boolean) {
  groupPrefs.value[id] = !isGroupCollapsed(id, defaultCollapsed)
}

function isGroupCollapsed(id: string, defaultCollapsed: boolean): boolean {
  return groupPrefs.value[id] ?? defaultCollapsed
}

export function useSidebar() {
  return {
    groupPrefs: readonly(groupPrefs),
    toggleGroup,
    isGroupCollapsed,
  }
}
