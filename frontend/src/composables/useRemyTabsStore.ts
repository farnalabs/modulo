import { defineStore } from 'pinia'
import { computed, watch } from 'vue'
import { useStorage } from '@vueuse/core'
import { useRemyStore } from './useRemyStore'

export interface RemyTab {
  tabId: string
  sessionId: string
}

const STORAGE_KEY = 'remy-only-tabs'

function makeTabId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // Fallback for environments without randomUUID — use the CSPRNG, not Math.random.
  const arr = new Uint8Array(16)
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(arr)
    return `tab-${Date.now()}-${Array.from(arr, (b) => b.toString(16).padStart(2, '0')).join('')}`
  }
  return `tab-${Date.now()}-${Math.random().toString(36).slice(2, 10)}` // NOSONAR: only when no CSPRNG exists
}

function parseStoredTabs(raw: unknown): RemyTab[] {
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (item): item is RemyTab =>
      typeof item === 'object' &&
      item !== null &&
      typeof (item as RemyTab).tabId === 'string' &&
      typeof (item as RemyTab).sessionId === 'string',
  )
}

export const useRemyTabsStore = defineStore('remyTabs', () => {
  const remyStore = useRemyStore()
  let _seeded = false

  const tabs = useStorage<RemyTab[]>(STORAGE_KEY, [], undefined, {
    serializer: {
      read: (v: string) => {
        try {
          const parsed: unknown = JSON.parse(v)
          return parseStoredTabs(parsed)
        } catch {
          console.warn('[RemyTabs] Corrupt remy-only tabs in localStorage — resetting')
          return []
        }
      },
      write: (v: RemyTab[]) => JSON.stringify(v),
    },
  })

  const activeTab = computed(
    () => tabs.value.find(t => t.sessionId === remyStore.activeSessionId) ?? null,
  )

  async function addTab() {
    const session = await remyStore.createSession()
    if (!session) return null
    tabs.value = [
      ...tabs.value.filter(t => t.sessionId !== session.id),
      { tabId: makeTabId(), sessionId: session.id },
    ]
    return session
  }

  async function resumeTab(sessionId: string) {
    if (!tabs.value.some(t => t.sessionId === sessionId)) {
      tabs.value = [...tabs.value, { tabId: makeTabId(), sessionId }]
    }
    await remyStore.loadSession(sessionId)
  }

  function closeTab(tabId: string) {
    const idx = tabs.value.findIndex(t => t.tabId === tabId)
    if (idx === -1) return
    const closing = tabs.value[idx]
    const wasActive = closing.sessionId === remyStore.activeSessionId
    tabs.value = tabs.value.filter(t => t.tabId !== tabId)
    if (wasActive) {
      if (tabs.value.length === 0) {
        remyStore.activeSessionId = null
        remyStore.messages = []
      } else {
        const next = tabs.value[Math.min(idx, tabs.value.length - 1)]
        remyStore.loadSession(next.sessionId)
      }
    }
  }

  function reconcile() {
    const live = Array.isArray(remyStore.sessions) ? remyStore.sessions : []
    const pruned = tabs.value.filter(t => live.some(s => s.id === t.sessionId))
    tabs.value = pruned

    if (!_seeded) {
      _seeded = true
      // First mount with no tabs but a live restored activeSessionId — seed a
      // tab for it instead of nulling a panel session the user may still use.
      if (pruned.length === 0 && remyStore.activeSessionId && live.some(s => s.id === remyStore.activeSessionId)) {
        tabs.value = [{ tabId: makeTabId(), sessionId: remyStore.activeSessionId }]
        return
      }
    }

    if (!tabs.value.some(t => t.sessionId === remyStore.activeSessionId)) {
      if (tabs.value.length === 0) {
        remyStore.activeSessionId = null
        remyStore.messages = []
      } else {
        remyStore.activeSessionId = tabs.value[0].sessionId
      }
    }
  }

  watch(() => remyStore.sessions, reconcile)

  return {
    tabs,
    activeTab,
    addTab,
    resumeTab,
    closeTab,
    reconcile,
  }
})
