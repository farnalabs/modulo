import { defineStore } from 'pinia'
import { computed, watch } from 'vue'
import { useStorage } from '@vueuse/core'
import { useAssistantStore } from './useAssistantStore'

export interface AssistantTab {
  tabId: string
  sessionId: string
}

const STORAGE_KEY = 'assistant-only-tabs'

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

function parseStoredTabs(raw: unknown): AssistantTab[] {
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (item): item is AssistantTab =>
      typeof item === 'object' &&
      item !== null &&
      typeof (item as AssistantTab).tabId === 'string' &&
      typeof (item as AssistantTab).sessionId === 'string',
  )
}

export const useAssistantTabsStore = defineStore('assistantTabs', () => {
  const assistantStore = useAssistantStore()
  let _seeded = false

  const tabs = useStorage<AssistantTab[]>(STORAGE_KEY, [], undefined, {
    serializer: {
      read: (v: string) => {
        try {
          const parsed: unknown = JSON.parse(v)
          return parseStoredTabs(parsed)
        } catch {
          console.warn('[AssistantTabs] Corrupt assistant-only tabs in localStorage — resetting')
          return []
        }
      },
      write: (v: AssistantTab[]) => JSON.stringify(v),
    },
  })

  const activeTab = computed(
    () => tabs.value.find(t => t.sessionId === assistantStore.activeSessionId) ?? null,
  )

  async function addTab() {
    const session = await assistantStore.createSession()
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
    await assistantStore.loadSession(sessionId)
  }

  function closeTab(tabId: string) {
    const idx = tabs.value.findIndex(t => t.tabId === tabId)
    if (idx === -1) return
    const closing = tabs.value[idx]
    const wasActive = closing.sessionId === assistantStore.activeSessionId
    tabs.value = tabs.value.filter(t => t.tabId !== tabId)
    if (wasActive) {
      if (tabs.value.length === 0) {
        assistantStore.activeSessionId = null
        assistantStore.messages = []
      } else {
        const next = tabs.value[Math.min(idx, tabs.value.length - 1)]
        assistantStore.loadSession(next.sessionId)
      }
    }
  }

  function reconcile() {
    const live = Array.isArray(assistantStore.sessions) ? assistantStore.sessions : []
    const pruned = tabs.value.filter(t => live.some(s => s.id === t.sessionId))
    tabs.value = pruned

    if (!_seeded) {
      _seeded = true
      // First mount with no tabs but a live restored activeSessionId — seed a
      // tab for it instead of nulling a panel session the user may still use.
      if (pruned.length === 0 && assistantStore.activeSessionId && live.some(s => s.id === assistantStore.activeSessionId)) {
        tabs.value = [{ tabId: makeTabId(), sessionId: assistantStore.activeSessionId }]
        return
      }
    }

    if (!tabs.value.some(t => t.sessionId === assistantStore.activeSessionId)) {
      if (tabs.value.length === 0) {
        assistantStore.activeSessionId = null
        assistantStore.messages = []
      } else {
        assistantStore.activeSessionId = tabs.value[0].sessionId
      }
    }
  }

  watch(() => assistantStore.sessions, reconcile)

  return {
    tabs,
    activeTab,
    addTab,
    resumeTab,
    closeTab,
    reconcile,
  }
})
