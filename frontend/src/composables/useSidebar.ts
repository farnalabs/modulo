import { ref } from 'vue'

const VIEW_MODE_KEY = 'modulo-sidebar-view-mode'
const PREF_KEY = 'modulo-sidebar-group-prefs'
const OLD_COLLAPSED_KEY = 'modulo-sidebar-collapsed-groups'

const viewMode = ref<'simple' | 'advanced'>('simple')
const groupPrefs = ref<Record<string, boolean>>({})

function init() {
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
  const pref = groupPrefs.value[id]
  groupPrefs.value[id] = pref === undefined ? !defaultCollapsed : !pref
  save()
}

function isGroupCollapsed(id: string, defaultCollapsed: boolean): boolean {
  const pref = groupPrefs.value[id]
  return pref !== undefined ? pref : defaultCollapsed
}

function setViewMode(mode: 'simple' | 'advanced') {
  viewMode.value = mode
  save()
}

export function useSidebar() {
  init()
  return {
    viewMode,
    groupPrefs,
    toggleGroup,
    isGroupCollapsed,
    setViewMode,
  }
}
