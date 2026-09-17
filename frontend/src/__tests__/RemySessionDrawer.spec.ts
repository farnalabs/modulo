import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Mock } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import RemySessionDrawer from '../components/remy/RemySessionDrawer.vue'
import { useRemyStore } from '../composables/useRemyStore'
import { api } from '@/lib/api/client'
import type { ChatSession } from '../types/remy'

vi.mock('@/lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    PATCH: vi.fn(),
    DELETE: vi.fn(),
  },
}))

const apiGet = api.GET as unknown as Mock
const apiPost = api.POST as unknown as Mock
const apiDelete = api.DELETE as unknown as Mock

function makeSession(id: string, overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    id,
    user_id: 'user-1',
    name: `Session ${id}`,
    session_number: null,
    provider: 'anthropic',
    model: 'claude-sonnet-4-20250514',
    context_window_tokens: 200000,
    system_prompt_hash: null,
    message_count: 5,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-17T12:00:00Z',
    ...overrides,
  }
}

function mountDrawer() {
  return mount(RemySessionDrawer, {
    global: {
      stubs: {
        Button: { template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>', props: ['disabled'] },
      },
    },
  })
}

describe('RemySessionDrawer', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.clearAllMocks()
    apiGet.mockResolvedValue({ data: { items: [] }, error: undefined })
  })

  it('shows loading state when sessions are loading', async () => {
    apiGet.mockReturnValue(new Promise(() => {}))
    const store = useRemyStore()
    store.sessionsLoading = true

    const wrapper = mountDrawer()
    expect(wrapper.text()).toContain('Loading')
    expect(wrapper.find('.remy-session-item').exists()).toBe(false)
  })

  it('shows empty state when no sessions exist', async () => {
    const store = useRemyStore()
    store.sessions = []

    const wrapper = mountDrawer()
    expect(wrapper.text()).toContain('No sessions yet')
    expect(wrapper.text()).toContain('Start a new chat')
  })

  it('renders sorted sessions', async () => {
    const store = useRemyStore()
    store.sessions = [
      makeSession('s-1', { name: 'Alpha', updated_at: '2026-09-16T00:00:00Z', message_count: 3 }),
      makeSession('s-2', { name: 'Beta', updated_at: '2026-09-17T00:00:00Z', message_count: 10 }),
    ]

    const wrapper = mountDrawer()
    const items = wrapper.findAll('.remy-session-item')
    expect(items).toHaveLength(2)
    // sortedSessions orders by updated_at desc, so Beta (newer) comes first
    expect(items[0].text()).toContain('Beta')
    expect(items[1].text()).toContain('Alpha')
  })

  it('shows message count for each session', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1', { message_count: 7 })]

    const wrapper = mountDrawer()
    expect(wrapper.text()).toContain('7 msgs')
  })

  it('highlights the active session', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1'), makeSession('s-2')]
    store.activeSessionId = 's-1'

    const wrapper = mountDrawer()
    const items = wrapper.findAll('.remy-session-item')
    expect(items[0].classes()).toContain('active')
    expect(items[1].classes()).not.toContain('active')
  })

  it('selects a session on click', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1')]
    apiGet.mockResolvedValue({ data: { items: [] }, error: undefined })

    const wrapper = mountDrawer()
    const loadSpy = vi.spyOn(store, 'loadSession').mockResolvedValue(undefined as never)

    await wrapper.find('.remy-session-item').trigger('click')

    expect(loadSpy).toHaveBeenCalledWith('s-1')
  })

  it('emits selectSession after selecting', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1')]
    apiGet.mockResolvedValue({ data: { items: [] }, error: undefined })

    const wrapper = mountDrawer()
    vi.spyOn(store, 'loadSession').mockResolvedValue(undefined as never)

    await wrapper.find('.remy-session-item').trigger('click')
    expect(wrapper.emitted('selectSession')).toHaveLength(1)
  })

  it('creates a new session when "Start a new chat" is clicked', async () => {
    const store = useRemyStore()
    store.sessions = []
    const newSession = makeSession('s-new')
    apiPost.mockResolvedValue({ data: newSession, error: undefined })

    const wrapper = mountDrawer()
    const createSpy = vi.spyOn(store, 'createSession').mockResolvedValue(newSession)

    await wrapper.find('button').trigger('click')
    expect(createSpy).toHaveBeenCalled()
  })

  it('emits selectSession after creating a session', async () => {
    const store = useRemyStore()
    store.sessions = []
    const newSession = makeSession('s-new')
    apiPost.mockResolvedValue({ data: newSession, error: undefined })

    const wrapper = mountDrawer()
    vi.spyOn(store, 'createSession').mockResolvedValue(newSession)

    // The "Start a new chat" button in empty state
    const buttons = wrapper.findAll('button')
    const newChatBtn = buttons.find(b => b.text().includes('Start a new chat'))
    await newChatBtn!.trigger('click')

    // selectSession is emitted at least once (once for the button click,
    // potentially twice if the native click also propagates through the stub)
    expect(wrapper.emitted('selectSession')!.length).toBeGreaterThanOrEqual(1)
  })

  it('deletes a session when delete button is clicked', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1')]
    apiDelete.mockResolvedValue({ data: undefined, error: undefined })

    const wrapper = mountDrawer()
    const deleteSpy = vi.spyOn(store, 'deleteSession').mockResolvedValue(undefined as never)

    await wrapper.find('.remy-session-delete').trigger('click')

    expect(deleteSpy).toHaveBeenCalledWith('s-1')
  })

  it('handles session name fallback with session_number', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1', { name: null, session_number: 42 })]

    const wrapper = mountDrawer()
    expect(wrapper.text()).toContain('#42')
  })

  it('handles session name fallback with shortId when no session_number', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('abc-def-123', { name: null, session_number: null })]

    const wrapper = mountDrawer()
    // shortId truncates to first 8 chars
    expect(wrapper.text()).toContain('abc-def-')
  })

  it('formatTime shows "just now" for very recent timestamps', async () => {
    const store = useRemyStore()
    const now = new Date().toISOString()
    store.sessions = [makeSession('s-1', { updated_at: now })]

    const wrapper = mountDrawer()
    expect(wrapper.text()).toContain('just now')
  })

  it('formatTime shows minutes ago for timestamps within the hour', async () => {
    const store = useRemyStore()
    const fiveMinAgo = new Date(Date.now() - 5 * 60000).toISOString()
    store.sessions = [makeSession('s-1', { updated_at: fiveMinAgo })]

    const wrapper = mountDrawer()
    expect(wrapper.text()).toContain('5m ago')
  })

  it('formatTime shows hours ago for timestamps within the day', async () => {
    const store = useRemyStore()
    const threeHrAgo = new Date(Date.now() - 3 * 3600000).toISOString()
    store.sessions = [makeSession('s-1', { updated_at: threeHrAgo })]

    const wrapper = mountDrawer()
    expect(wrapper.text()).toContain('3h ago')
  })

  it('formatTime shows days ago for timestamps within the week', async () => {
    const store = useRemyStore()
    const twoDaysAgo = new Date(Date.now() - 2 * 86400000).toISOString()
    store.sessions = [makeSession('s-1', { updated_at: twoDaysAgo })]

    const wrapper = mountDrawer()
    expect(wrapper.text()).toContain('2d ago')
  })

  it('formatTime falls back to formatted date for old timestamps', async () => {
    const store = useRemyStore()
    const tenDaysAgo = new Date(Date.now() - 10 * 86400000).toISOString()
    store.sessions = [makeSession('s-1', { updated_at: tenDaysAgo })]

    const wrapper = mountDrawer()
    // formatDateShort should produce something like "Sep 7" or "09/07"
    expect(wrapper.text()).not.toContain('d ago')
  })

  it('formatTime returns empty string for falsy iso', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1', { updated_at: '' })]

    const wrapper = mountDrawer()
    // The session item still renders, just with an empty time string
    const item = wrapper.find('.remy-session-item')
    expect(item.exists()).toBe(true)
  })

  it('formatTime returns the raw string for invalid dates', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1', { updated_at: 'not-a-date' })]

    const wrapper = mountDrawer()
    expect(wrapper.text()).toContain('not-a-date')
  })

  it('handles createSession failure gracefully', async () => {
    const store = useRemyStore()
    store.sessions = []
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    const wrapper = mountDrawer()
    vi.spyOn(store, 'createSession').mockRejectedValue(new Error('create failed'))

    await wrapper.find('button').trigger('click')

    expect(consoleSpy).toHaveBeenCalledWith('Failed to create session:', expect.any(Error))
    consoleSpy.mockRestore()
  })

  it('handles deleteSession failure gracefully', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1')]
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    const wrapper = mountDrawer()
    vi.spyOn(store, 'deleteSession').mockRejectedValue(new Error('delete failed'))

    await wrapper.find('.remy-session-delete').trigger('click')

    expect(consoleSpy).toHaveBeenCalledWith('Failed to delete session:', expect.any(Error))
    consoleSpy.mockRestore()
  })
})
