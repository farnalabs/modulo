import { ref, readonly } from 'vue'

const VIEW_MODE_KEY = 'modulo-sidebar-view-mode'
const PREF_KEY = 'modulo-sidebar-group-prefs'
const OLD_COLLAPSED_KEY = 'modulo-sidebar-collapsed-groups'

const viewMode = ref<'simple' | 'advanced'>('simple')
const groupPrefs = ref<Record<string, boolean>>({})

let initialized = false

function init() {
  if (initialized) return
  initialized = true

  const saved = localStorage.getItem(VIEW_MODE_KEY)
  if (saved === 'simple' || saved === 'advanced') {
    viewMode.value = saved
  }

  const prefs = localStorage.getItem(PREF_KEY)
  if (prefs) {
    try {
      const obj = JSON.parse(prefs)
      if (typeof obj === 'object' && obj !== null) {
        groupPrefs.value = obj
      }
    } catch {
      // ignore
    }
  } else {
    const old = localStorage.getItem(OLD_COLLAPSED_KEY)
    if (old) {
      try {
        const arr = JSON.parse(old)
        if (Array.isArray(arr)) {
          const migrated: Record<string, boolean> = {}
          for (const id of arr) {
            migrated[id] = true
          }
          groupPrefs.value = migrated
          save()
        }
      } catch {
        // ignore
      }
      localStorage.removeItem(OLD_COLLAPSED_KEY)
    }
  }
}

function save() {
  localStorage.setItem(VIEW_MODE_KEY, viewMode.value)
  localStorage.setItem(PREF_KEY, JSON.stringify(groupPrefs.value))
}

function toggleGroup(id: string, defaultCollapsed: boolean) {
  groupPrefs.value[id] = !isGroupCollapsed(id, defaultCollapsed)
  save()
}

function isGroupCollapsed(id: string, defaultCollapsed: boolean): boolean {
  return groupPrefs.value[id] ?? defaultCollapsed
}

function setViewMode(mode: 'simple' | 'advanced') {
  viewMode.value = mode
  save()
}

export function useSidebar() {
  init()
  return {
    viewMode: readonly(viewMode),
    groupPrefs: readonly(groupPrefs),
    toggleGroup,
    isGroupCollapsed,
    setViewMode,
  }
}
