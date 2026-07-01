import { ref } from 'vue'

const VIEW_MODE_KEY = 'modulo-sidebar-view-mode'
const COLLAPSED_KEY = 'modulo-sidebar-collapsed-groups'

const viewMode = ref<'simple' | 'advanced'>('simple')
const collapsedGroups = ref<Set<string>>(new Set())

function init() {
  const saved = localStorage.getItem(VIEW_MODE_KEY)
  if (saved === 'simple' || saved === 'advanced') {
    viewMode.value = saved
  }
  const collapsed = localStorage.getItem(COLLAPSED_KEY)
  if (collapsed) {
    try {
      const arr = JSON.parse(collapsed)
      if (Array.isArray(arr)) {
        collapsedGroups.value = new Set(arr)
      }
    } catch {
      // ignore
    }
  }
}

function save() {
  localStorage.setItem(VIEW_MODE_KEY, viewMode.value)
  localStorage.setItem(COLLAPSED_KEY, JSON.stringify(Array.from(collapsedGroups.value)))
}

function toggleGroup(id: string) {
  if (collapsedGroups.value.has(id)) {
    collapsedGroups.value.delete(id)
  } else {
    collapsedGroups.value.add(id)
  }
  save()
}

function isGroupCollapsed(id: string, defaultCollapsed: boolean): boolean {
  if (collapsedGroups.value.has(id)) return true
  if (!collapsedGroups.value.has(id) && collapsedGroups.value.size > 0) return false
  return defaultCollapsed
}

function setViewMode(mode: 'simple' | 'advanced') {
  viewMode.value = mode
  save()
}

export function useSidebar() {
  init()
  return {
    viewMode,
    collapsedGroups,
    toggleGroup,
    isGroupCollapsed,
    setViewMode,
  }
}
