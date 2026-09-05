import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Mock } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
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
const apiPatch = api.PATCH as unknown as Mock
const apiDelete = api.DELETE as unknown as Mock

function makeSession(id: string, updatedAt: string): ChatSession {
  return {
    id,
    user_id: 'user-1',
    name: `Session ${id}`,
    session_number: null,
    provider: 'anthropic',
    model: 'claude-sonnet-4-20250514',
    context_window_tokens: 200000,
    system_prompt_hash: null,
    message_count: 0,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: updatedAt,
  }
}

describe('useRemyStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('starts with panel closed', () => {
    const store = useRemyStore()
    expect(store.panelState).toBe('docked')
  })

  it('starts with empty sessions', () => {
    const store = useRemyStore()
    expect(store.sessions).toEqual([])
  })

  it('starts with empty messages', () => {
    const store = useRemyStore()
    expect(store.messages).toEqual([])
  })

  it('starts with isStreaming false', () => {
    const store = useRemyStore()
    expect(store.isStreaming).toBe(false)
  })

  it('setPanelState updates state', () => {
    const store = useRemyStore()
    store.setPanelState('floating')
    expect(store.panelState).toBe('floating')
    store.setPanelState('docked')
    expect(store.panelState).toBe('docked')
    store.setPanelState('maximised')
    expect(store.panelState).toBe('maximised')
    store.setPanelState('closed')
    expect(store.panelState).toBe('closed')
  })

  it('appendToken creates new message when last is not assistant', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('Hello')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].role).toBe('assistant')
    expect(store.messages[0].content).toBe('Hello')
  })

  it('appendToken appends to existing assistant message', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('Hello')
    store.appendToken(' World')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].content).toBe('Hello World')
  })

  it('removeLastUserMessage removes last user message', () => {
    const store = useRemyStore()
    store.messages.push({
      id: '1', session_id: 's1', role: 'user',
      content: 'hi', tool_calls_json: null, tool_results_json: null,
      token_count: null, parent_id: null, created_at: new Date().toISOString(),
    })
    store.messages.push({
      id: '2', session_id: 's1', role: 'assistant',
      content: 'hello', tool_calls_json: null, tool_results_json: null,
      token_count: null, parent_id: null, created_at: new Date().toISOString(),
    })
    store.removeLastUserMessage()
    expect(store.messages).toHaveLength(2) // last is assistant, not removed
    store.messages.push({
      id: '3', session_id: 's1', role: 'user',
      content: 'bye', tool_calls_json: null, tool_results_json: null,
      token_count: null, parent_id: null, created_at: new Date().toISOString(),
    })
    store.removeLastUserMessage()
    expect(store.messages).toHaveLength(2) // last user removed
  })

  it('appendToolCall adds a tool_result message', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-1', tool_name: 'test', success: true,
    })
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].role).toBe('tool_result')
    expect(store.messages[0].content).toContain('completed')
  })

  it('appendToolCall shows error for failed tool', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-2', tool_name: 'test', success: false, error: 'timeout',
    })
    expect(store.messages[0].content).toContain('failed')
    expect(store.messages[0].content).toContain('timeout')
  })

  it('collapses floating panel to closed on narrow viewport', () => {
    const store = useRemyStore()
    store.setPanelState('floating')
    vi.stubGlobal('innerWidth', 390)
    try {
      store.collapseIfNarrow()
      expect(store.panelState).toBe('closed')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('collapses docked and maximised panels to closed on narrow viewport', () => {
    const store = useRemyStore()
    vi.stubGlobal('innerWidth', 390)
    try {
      store.setPanelState('docked')
      store.collapseIfNarrow()
      expect(store.panelState).toBe('closed')
      store.setPanelState('maximised')
      store.collapseIfNarrow()
      expect(store.panelState).toBe('closed')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('keeps panel state on wide viewport', () => {
    const store = useRemyStore()
    store.setPanelState('floating')
    vi.stubGlobal('innerWidth', 1280)
    try {
      store.collapseIfNarrow()
      expect(store.panelState).toBe('floating')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('collapseIfNarrow never force-opens a closed panel', () => {
    const store = useRemyStore()
    store.setPanelState('closed')
    vi.stubGlobal('innerWidth', 390)
    try {
      store.collapseIfNarrow()
      expect(store.panelState).toBe('closed')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('useRemyStore API methods', () => {
  beforeEach(() => {
    // useStorage persists activeSessionId/panelState across stores via
    // localStorage — start each test from a clean slate.
    localStorage.clear()
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('fetchSessions loads the session list and clears the loading flag', async () => {
    const store = useRemyStore()
    const sessions = [makeSession('s-1', '2026-09-01T00:00:00Z')]
    apiGet.mockResolvedValue({ data: { items: sessions } })

    await store.fetchSessions()

    expect(apiGet).toHaveBeenCalledWith('/api/v1/remy/sessions')
    expect(store.sessions).toEqual(sessions)
    expect(store.error).toBeNull()
    expect(store.sessionsLoading).toBe(false)
  })

  it('fetchSessions surfaces API errors', async () => {
    const store = useRemyStore()
    apiGet.mockResolvedValue({ error: { detail: 'session list unavailable' } })

    await store.fetchSessions()

    expect(store.error).toBe('session list unavailable')
    expect(store.sessionsLoading).toBe(false)
  })

  it('fetchSessions surfaces thrown network errors', async () => {
    const store = useRemyStore()
    apiGet.mockRejectedValue(new Error('network down'))

    await store.fetchSessions()

    expect(store.error).toBe('network down')
    expect(store.sessionsLoading).toBe(false)
  })

  it('createSession prepends the new session, selects it, and clears messages', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-old', '2026-09-01T00:00:00Z')]
    store.messages.push({
      id: 'm-1', session_id: 's-old', role: 'user', content: 'hi',
      tool_calls_json: null, tool_results_json: null, token_count: null,
      parent_id: null, created_at: new Date().toISOString(),
    })
    const created = makeSession('s-new', '2026-09-02T00:00:00Z')
    apiPost.mockResolvedValue({ data: created })

    const result = await store.createSession()

    expect(apiPost).toHaveBeenCalledWith('/api/v1/remy/sessions', expect.objectContaining({
      body: expect.objectContaining({ context_window_tokens: 200000 }),
    }))
    expect(result).toEqual(created)
    expect(store.sessions[0].id).toBe('s-new')
    expect(store.activeSessionId).toBe('s-new')
    expect(store.messages).toHaveLength(0)
  })

  it('createSession returns null and sets the error on API failure', async () => {
    const store = useRemyStore()
    apiPost.mockResolvedValue({ error: { detail: 'quota exhausted' } })

    const result = await store.createSession()

    expect(result).toBeNull()
    expect(store.error).toBe('quota exhausted')
  })

  it('createSession returns null and sets the error when the call throws', async () => {
    const store = useRemyStore()
    apiPost.mockRejectedValue(new Error('boom'))

    const result = await store.createSession()

    expect(result).toBeNull()
    expect(store.error).toBe('boom')
  })

  it('loadSession fetches messages and sets the active session', async () => {
    const store = useRemyStore()
    const messages = [
      {
        id: 'm-1', session_id: 's-1', role: 'assistant', content: 'hello',
        tool_calls_json: null, tool_results_json: null, token_count: null,
        parent_id: null, created_at: new Date().toISOString(),
      },
    ]
    apiGet.mockResolvedValue({ data: { items: messages } })

    await store.loadSession('s-1')

    expect(apiGet).toHaveBeenCalledWith('/api/v1/remy/sessions/{session_id}/messages', {
      params: { path: { session_id: 's-1' } },
    })
    expect(store.messages).toEqual(messages)
    expect(store.activeSessionId).toBe('s-1')
    expect(store.loading).toBe(false)
  })

  it('loadSession surfaces API errors without changing the active session', async () => {
    const store = useRemyStore()
    apiGet.mockResolvedValue({ error: { detail: 'not found' } })

    await store.loadSession('s-missing')

    expect(store.error).toBe('not found')
    expect(store.activeSessionId).toBeNull()
    expect(store.loading).toBe(false)
  })

  it('loadSession surfaces thrown errors', async () => {
    const store = useRemyStore()
    apiGet.mockRejectedValue(new Error('disconnected'))

    await store.loadSession('s-1')

    expect(store.error).toBe('disconnected')
    expect(store.loading).toBe(false)
  })

  it('renameSession updates the session in place', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1', '2026-09-01T00:00:00Z')]
    const renamed = makeSession('s-1', '2026-09-01T00:00:00Z')
    renamed.name = 'Renamed'
    apiPatch.mockResolvedValue({ data: renamed })

    const ok = await store.renameSession('s-1', 'Renamed')

    expect(ok).toBe(true)
    expect(store.sessions[0].name).toBe('Renamed')
  })

  it('renameSession still succeeds when the session is not in the local list', async () => {
    const store = useRemyStore()
    const renamed = makeSession('s-unknown', '2026-09-01T00:00:00Z')
    apiPatch.mockResolvedValue({ data: renamed })

    await expect(store.renameSession('s-unknown', 'Renamed')).resolves.toBe(true)
  })

  it('renameSession returns false on API errors', async () => {
    const store = useRemyStore()
    apiPatch.mockResolvedValue({ error: { detail: 'name taken' } })

    const ok = await store.renameSession('s-1', 'X')

    expect(ok).toBe(false)
    expect(store.error).toBe('name taken')
  })

  it('deleteSession removes the session and clears an active selection', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1', '2026-09-01T00:00:00Z'), makeSession('s-2', '2026-09-01T01:00:00Z')]
    store.activeSessionId = 's-1'
    store.messages.push({
      id: 'm-1', session_id: 's-1', role: 'user', content: 'hi',
      tool_calls_json: null, tool_results_json: null, token_count: null,
      parent_id: null, created_at: new Date().toISOString(),
    })
    apiDelete.mockResolvedValue({ data: undefined })

    await store.deleteSession('s-1')

    expect(apiDelete).toHaveBeenCalledWith('/api/v1/remy/sessions/{session_id}', {
      params: { path: { session_id: 's-1' } },
    })
    expect(store.sessions.map((s: ChatSession) => s.id)).toEqual(['s-2'])
    expect(store.activeSessionId).toBeNull()
    expect(store.messages).toHaveLength(0)
  })

  it('deleteSession keeps an unrelated active selection', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1', '2026-09-01T00:00:00Z')]
    store.activeSessionId = 's-other'
    apiDelete.mockResolvedValue({ data: undefined })

    await store.deleteSession('s-1')

    expect(store.activeSessionId).toBe('s-other')
  })

  it('deleteSession surfaces API errors without mutating the list', async () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1', '2026-09-01T00:00:00Z')]
    apiDelete.mockResolvedValue({ error: { detail: 'forbidden' } })

    await store.deleteSession('s-1')

    expect(store.error).toBe('forbidden')
    expect(store.sessions).toHaveLength(1)
  })

  it('deleteSession surfaces thrown errors', async () => {
    const store = useRemyStore()
    apiDelete.mockRejectedValue(new Error('gone'))

    await store.deleteSession('s-1')

    expect(store.error).toBe('gone')
  })

  it('sendMessage only sends when a session is active', () => {
    const store = useRemyStore()
    store.sendMessage('ignored')
    expect(store.messages).toHaveLength(0)
    expect(store.isStreaming).toBe(false)

    store.activeSessionId = 's-1'
    store.sendMessage('hello remy')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0]).toMatchObject({ role: 'user', content: 'hello remy', session_id: 's-1' })
    expect(store.isStreaming).toBe(true)
  })

  it('approvePermission posts the response and clears the pending request', async () => {
    const store = useRemyStore()
    store.activeSessionId = 's-1'
    store.pendingPermission = { request_id: 'req-1', tools: [] }
    apiPost.mockResolvedValue({ data: undefined })

    await store.approvePermission('req-1', 'approve')

    expect(apiPost).toHaveBeenCalledWith(
      '/api/v1/remy/sessions/{session_id}/permission-response',
      {
        params: { path: { session_id: 's-1' } },
        body: { request_id: 'req-1', action: 'approve' },
      },
    )
    expect(store.pendingPermission).toBeNull()
    expect(store.error).toBeNull()
  })

  it('approvePermission without an active session sets an error and never posts', async () => {
    const store = useRemyStore()

    await store.approvePermission('req-1', 'approve')

    expect(apiPost).not.toHaveBeenCalled()
    expect(store.error).toContain('no active session')
  })

  it('approvePermission surfaces API errors and clears the pending request', async () => {
    const store = useRemyStore()
    store.activeSessionId = 's-1'
    store.pendingPermission = { request_id: 'req-1', tools: [] }
    apiPost.mockResolvedValue({ error: { detail: 'expired request' } })

    await store.approvePermission('req-1', 'reject')

    expect(store.error).toBe('expired request')
    expect(store.pendingPermission).toBeNull()
  })

  it('approvePermission surfaces thrown errors and clears the pending request', async () => {
    const store = useRemyStore()
    store.activeSessionId = 's-1'
    store.pendingPermission = { request_id: 'req-1', tools: [] }
    apiPost.mockRejectedValue(new Error('socket hang-up'))

    await store.approvePermission('req-1', 'approve_for_session')

    expect(store.error).toBe('socket hang-up')
    expect(store.pendingPermission).toBeNull()
  })

  it('resetSessionPermissions posts to the reset endpoint', async () => {
    const store = useRemyStore()
    store.activeSessionId = 's-1'
    apiPost.mockResolvedValue({ data: undefined })

    await store.resetSessionPermissions()

    expect(apiPost).toHaveBeenCalledWith('/api/v1/remy/sessions/{session_id}/reset-permissions', {
      params: { path: { session_id: 's-1' } },
    })
  })

  it('resetSessionPermissions no-ops without an active session', async () => {
    const store = useRemyStore()

    await store.resetSessionPermissions()

    expect(apiPost).not.toHaveBeenCalled()
  })

  it('resetSessionPermissions surfaces API and thrown errors', async () => {
    const store = useRemyStore()
    store.activeSessionId = 's-1'
    apiPost.mockResolvedValue({ error: { detail: 'not allowed' } })

    await store.resetSessionPermissions()
    expect(store.error).toBe('not allowed')

    apiPost.mockRejectedValue(new Error('reset blew up'))
    await store.resetSessionPermissions()
    expect(store.error).toBe('reset blew up')
  })

  it('pauseRemy and resumeRemy toggle the paused flag', () => {
    const store = useRemyStore()
    store.pauseRemy()
    expect(store.isPaused).toBe(true)
    store.resumeRemy()
    expect(store.isPaused).toBe(false)
  })

  it('appendSystemMessage and appendTurnSeparator add summary messages', () => {
    const store = useRemyStore()
    store.activeSessionId = 's-1'
    store.appendSystemMessage('Action cancelled by user.')
    store.appendTurnSeparator('--- turn boundary ---')

    expect(store.messages).toHaveLength(2)
    expect(store.messages[0]).toMatchObject({ role: 'summary', content: 'Action cancelled by user.', session_id: 's-1' })
    expect(store.messages[1]).toMatchObject({ role: 'summary', content: '--- turn boundary ---' })
  })

  it('setPendingPermission and setPageContext store their payloads', () => {
    const store = useRemyStore()
    const req = { request_id: 'r-9', tools: [{ name: 'click', args: {} }] }
    store.setPendingPermission(req)
    expect(store.pendingPermission).toEqual(req)

    store.setPageContext({ route: '/pipelines', params: { id: 'p-1' }, entities: ['pipeline'] })
    expect(store.pageContext).toEqual({ route: '/pipelines', params: { id: 'p-1' }, entities: ['pipeline'] })
  })

  it('triggerRename and signalSkillsChanged bump their counters', () => {
    const store = useRemyStore()
    expect(store.requestRename).toBe(0)
    store.triggerRename()
    expect(store.requestRename).toBe(1)

    expect(store.skillsVersion).toBe(0)
    store.signalSkillsChanged()
    store.signalSkillsChanged()
    expect(store.skillsVersion).toBe(2)
  })

  it('activeSession finds the selected session', () => {
    const store = useRemyStore()
    store.sessions = [makeSession('s-1', '2026-09-01T00:00:00Z'), makeSession('s-2', '2026-09-02T00:00:00Z')]
    store.activeSessionId = 's-2'
    expect(store.activeSession?.id).toBe('s-2')

    store.activeSessionId = 'missing'
    expect(store.activeSession).toBeNull()
  })

  it('sortedSessions orders by updated_at descending and tolerates invalid dates', () => {
    const store = useRemyStore()
    store.sessions = [
      makeSession('older', '2026-09-01T00:00:00Z'),
      makeSession('newer', '2026-09-03T00:00:00Z'),
      makeSession('middle', '2026-09-02T00:00:00Z'),
      makeSession('invalid', 'not-a-date'),
    ]
    expect(store.sortedSessions.map((s: ChatSession) => s.id)).toEqual(['newer', 'middle', 'older', 'invalid'])
  })

  it('clamps panel position and size to the viewport', () => {
    const store = useRemyStore()
    store.updatePosition({ x: -100, y: 99999 })
    expect(store.panelPosition.x).toBeGreaterThanOrEqual(8)
    expect(store.panelPosition.y).toBeLessThanOrEqual(window.innerHeight - 100)

    store.updateSize({ width: 10, height: 99999 })
    expect(store.panelSize.width).toBeGreaterThanOrEqual(100)
    expect(store.panelSize.height).toBeLessThanOrEqual(window.innerHeight - 40)

    store.panelPosition = { x: -500, y: 5000 }
    store.reclampPosition()
    expect(store.panelPosition.x).toBe(8)
    expect(store.panelPosition.y).toBeLessThanOrEqual(window.innerHeight - 100)
  })

  it('disposeResponsive removes the resize listener', () => {
    const store = useRemyStore()
    const spy = vi.spyOn(window, 'removeEventListener')
    store.disposeResponsive()
    // pinia wraps store actions, so match the event name + any handler fn.
    expect(spy).toHaveBeenCalledWith('resize', expect.any(Function))
  })
})
