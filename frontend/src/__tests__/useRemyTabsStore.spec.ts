import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useRemyStore } from '../composables/useRemyStore'
import { useRemyTabsStore } from '../composables/useRemyTabsStore'
import { api } from '@/lib/api/client'

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
  api: {
    POST: vi.fn(() => Promise.resolve({ error: null, data: { id: 'session-new', session_number: 1 } })),
    GET: vi.fn(() => Promise.resolve({ error: null, data: { items: [] } })),
    PATCH: vi.fn(() => Promise.resolve({ error: null, data: {} })),
    DELETE: vi.fn(() => Promise.resolve({ error: null, data: {} })),
  },
}))

vi.mock('../composables/useUiCommandExecutor', () => ({
  pauseUiCommands: vi.fn(),
  resumeUiCommands: vi.fn(),
  executeCommandBatch: vi.fn(),
  isPaused: vi.fn(() => false),
  abortUiCommands: vi.fn(),
}))

function makeSession(id: string, name: string) {
  return {
    id,
    user_id: 'user-1',
    name,
    session_number: 1,
    provider: 'anthropic',
    model: 'claude-sonnet-4-20250514',
    context_window_tokens: 200000,
    system_prompt_hash: null,
    message_count: 0,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }
}

function seedStoredTabs(entries: Array<[string, string]>) {
  localStorage.setItem(
    'remy-only-tabs',
    JSON.stringify(entries.map(([tabId, sessionId]) => ({ tabId, sessionId }))),
  )
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  vi.restoreAllMocks()
})

describe('useRemyTabsStore', () => {
  it('first-mount SEED: live restored activeSessionId with no tabs seeds a tab and does not null it', () => {
    const store = useRemyStore()
    store.sessions = [makeSession('session-9', 'Restored')]
    store.activeSessionId = 'session-9'
    const tabs = useRemyTabsStore()
    tabs.reconcile()
    expect(tabs.tabs).toHaveLength(1)
    expect(tabs.tabs[0].sessionId).toBe('session-9')
    expect(store.activeSessionId).toBe('session-9')
  })

  it('persists seeded tabs to localStorage and restores them on a fresh store instance', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('session-1', 'Alpha')]
    store.activeSessionId = 'session-1'
    const tabs = useRemyTabsStore()
    tabs.reconcile()
    const seeded = tabs.tabs
    expect(seeded).toHaveLength(1)
    expect(seeded[0].sessionId).toBe('session-1')

    await new Promise(resolve => setTimeout(resolve, 0))
    const stored = JSON.parse(localStorage.getItem('remy-only-tabs') || '[]')
    expect(stored).toHaveLength(1)
    expect(stored[0].sessionId).toBe('session-1')

    setActivePinia(createPinia())
    const store2 = useRemyStore()
    store2.sessions = [makeSession('session-1', 'Alpha')]
    const tabs2 = useRemyTabsStore()
    expect(tabs2.tabs).toEqual(seeded)
    expect(store2.activeSessionId).toBe('session-1')
  })

  it('addTab creates a session and appends a tab for it', async () => {
    const store = useRemyStore()
    const tabs = useRemyTabsStore()
    const session = await tabs.addTab()
    expect(session?.id).toBe('session-new')
    expect(tabs.tabs).toHaveLength(1)
    expect(tabs.tabs[0].sessionId).toBe('session-new')
    expect(store.activeSessionId).toBe('session-new')
  })

  it('addTab replaces an existing tab for the same session instead of duplicating', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('session-1', 'Alpha')]
    store.activeSessionId = 'session-1'
    const tabs = useRemyTabsStore()
    tabs.reconcile()
    expect(tabs.tabs).toHaveLength(1)
    const originalTabId = tabs.tabs[0].tabId

    ;(api.POST as any).mockResolvedValueOnce({ error: null, data: { id: 'session-1', session_number: 1 } })
    await tabs.addTab()
    expect(tabs.tabs).toHaveLength(1)
    expect(tabs.tabs[0].sessionId).toBe('session-1')
    expect(tabs.tabs[0].tabId).not.toBe(originalTabId)
  })

  it('resumeTab does not duplicate an existing tab for the session', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('session-1', 'Alpha')]
    store.activeSessionId = 'session-1'
    const tabs = useRemyTabsStore()
    tabs.reconcile()
    expect(tabs.tabs).toHaveLength(1)
    await tabs.resumeTab('session-1')
    expect(tabs.tabs).toHaveLength(1)
  })

  it('closeTab removes a non-active tab and keeps the active session intact', () => {
    seedStoredTabs([['t1', 'session-1'], ['t2', 'session-2']])
    const store = useRemyStore()
    store.sessions = [makeSession('session-1', 'Alpha'), makeSession('session-2', 'Beta')]
    store.activeSessionId = 'session-1'
    const tabs = useRemyTabsStore()
    tabs.reconcile()
    expect(tabs.tabs).toHaveLength(2)
    const closing = tabs.tabs.find(t => t.tabId === 't2')!
    tabs.closeTab(closing.tabId)
    expect(tabs.tabs).toHaveLength(1)
    expect(tabs.tabs[0].sessionId).toBe('session-1')
    expect(store.activeSessionId).toBe('session-1')
  })

  it('closing the ACTIVE tab reassigns to the next tab', () => {
    seedStoredTabs([['t1', 'session-1'], ['t2', 'session-2']])
    const store = useRemyStore()
    store.sessions = [makeSession('session-1', 'Alpha'), makeSession('session-2', 'Beta')]
    store.activeSessionId = 'session-1'
    const tabs = useRemyTabsStore()
    tabs.reconcile()
    const loadSessionSpy = vi.spyOn(store, 'loadSession').mockResolvedValue(undefined as never)
    tabs.closeTab('t1')
    expect(tabs.tabs.length).toBe(1)
    expect(tabs.tabs[0].sessionId).toBe('session-2')
    expect(loadSessionSpy).toHaveBeenCalledWith('session-2')
  })

  it('closing the LAST tab clears activeSessionId and messages', () => {
    seedStoredTabs([['t1', 'session-1']])
    const store = useRemyStore()
    store.sessions = [makeSession('session-1', 'Alpha')]
    store.activeSessionId = 'session-1'
    store.messages = [{ id: 'm1', session_id: 'session-1', role: 'user', content: 'hi', tool_calls_json: null, tool_results_json: null, token_count: null, parent_id: null, created_at: new Date().toISOString() }]
    const tabs = useRemyTabsStore()
    tabs.reconcile()
    tabs.closeTab('t1')
    expect(tabs.tabs.length).toBe(0)
    expect(store.activeSessionId).toBeNull()
    expect(store.messages).toEqual([])
  })

  it('corrupt JSON in localStorage resets tabs to an empty list', () => {
    localStorage.setItem('remy-only-tabs', 'not-json{{{')
    const tabs = useRemyTabsStore()
    expect(tabs.tabs).toEqual([])
  })

  it('active-with-no-tab but tabs exist reassigns active to the first live tab', () => {
    seedStoredTabs([['t1', 'session-1'], ['t2', 'session-2']])
    const store = useRemyStore()
    store.sessions = [makeSession('session-1', 'Alpha'), makeSession('session-2', 'Beta')]
    store.activeSessionId = 'ghost-session'
    const tabs = useRemyTabsStore()
    tabs.reconcile()
    expect(tabs.tabs.length).toBe(2)
    expect(store.activeSessionId).toBe('session-1')
  })

  it('tabs only persist tabId+sessionId — titles are derived, not stored', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('session-1', 'Alpha')]
    store.activeSessionId = 'session-1'
    const tabs = useRemyTabsStore()
    tabs.reconcile()
    const tab = tabs.tabs[0]
    expect(Object.keys(tab).sort()).toEqual(['sessionId', 'tabId'])
    await new Promise(resolve => setTimeout(resolve, 0))
    const stored = JSON.parse(localStorage.getItem('remy-only-tabs') || '[]')
    expect(stored[0].title).toBeUndefined()
  })
})
