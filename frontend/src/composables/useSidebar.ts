import { readonly } from 'vue'
import { useStorage } from '@vueuse/core'

export type ViewMode = 'simple' | 'advanced'

const groupPrefs = useStorage<Record<string, boolean>>('sidebar-group-prefs', {})
const collapsed = useStorage<boolean>('sidebar-collapsed', false)

function toggleGroup(id: string, defaultCollapsed: boolean) {
  groupPrefs.value[id] = !isGroupCollapsed(id, defaultCollapsed)
}

function isGroupCollapsed(id: string, defaultCollapsed: boolean): boolean {
  return groupPrefs.value[id] ?? defaultCollapsed
}

function setCollapsed(v: boolean) {
  collapsed.value = v
}

function toggleCollapsed() {
  collapsed.value = !collapsed.value
}

export function useSidebar() {
  return {
    groupPrefs: readonly(groupPrefs),
    collapsed: readonly(collapsed),
    toggleGroup,
    isGroupCollapsed,
    setCollapsed,
    toggleCollapsed,
  }
}
