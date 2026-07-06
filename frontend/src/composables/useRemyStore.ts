import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, getAuthHeaders } from '@/lib/api/client'
import { formatApiError } from '@/lib/api/formatError'
import type { ChatSession, ChatMessage, PageContext } from '@/types/remy'

export interface PermissionRequest {
  request_id: string
  tools: Array<{ name: string; args: Record<string, unknown> }>
}

const POSITION_KEY = 'remy_panel_position'
const SIZE_KEY = 'remy_panel_size'
const DEFAULT_CONTEXT_WINDOW_TOKENS = 200000

function loadPosition(): { x: number; y: number } {
  try {
    const raw = localStorage.getItem(POSITION_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      return {
        x: Math.max(8, Math.min(parsed.x, window.innerWidth - 340)),
        y: Math.max(8, Math.min(parsed.y, window.innerHeight - 100)),
      }
    }
  } catch {
    console.warn('[RemyStore] Failed to load panel position')
  }
  const defaultX = Math.max(8, window.innerWidth - 460)
  return { x: defaultX, y: 80 }
}

function loadSize(): { width: number; height: number } {
  try {
    const raw = localStorage.getItem(SIZE_KEY)
    if (raw) return JSON.parse(raw)
  } catch {
    console.warn('[RemyStore] Failed to load panel size')
  }
  const defaultWidth = Math.min(440, window.innerWidth - 16)
  const defaultHeight = Math.min(600, window.innerHeight - 120)
  return { width: defaultWidth, height: defaultHeight }
}

export function extractErrorMessage(err: unknown): string {
  return formatApiError(err)
}

function createMessage(role: ChatMessage['role'], content: string, overrides?: Partial<ChatMessage>): ChatMessage {
  return {
    id: `${role}-${Date.now()}`,
    session_id: '',
    role,
    content,
    tool_calls_json: null,
    tool_results_json: null,
    token_count: null,
    parent_id: null,
    created_at: new Date().toISOString(),
    ...overrides,
  }
}

export const useRemyStore = defineStore('remy', () => {
  const sessions = ref<ChatSession[]>([])
  const activeSessionId = ref<string | null>(null)
  const messages = ref<ChatMessage[]>([])
  const panelState = ref<'closed' | 'floating' | 'docked' | 'maximised'>('closed')
  const panelPosition = ref(loadPosition())
  const panelSize = ref(loadSize())
  const isStreaming = ref(false)
  const pageContext = ref<PageContext>({ route: '', params: {}, entities: [] })
  const loading = ref(false)
  const error = ref<string | null>(null)
  const sessionsLoading = ref(false)
  const pendingPermission = ref<PermissionRequest | null>(null)
  const isExecutingUi = ref(false)

  const activeSession = computed(() =>
    Array.isArray(sessions.value) ? sessions.value.find(s => s.id === activeSessionId.value) ?? null : null,
  )

  const sortedSessions = computed(() =>
    Array.isArray(sessions.value)
      ? [...sessions.value].sort((a, b) => {
          const ta = new Date(a.updated_at).getTime()
          const tb = new Date(b.updated_at).getTime()
          if (isNaN(ta) || isNaN(tb)) return 0
          return tb - ta
        })
      : [],
  )

  function persistPosition() {
    localStorage.setItem(POSITION_KEY, JSON.stringify(panelPosition.value))
  }

  function persistSize() {
    localStorage.setItem(SIZE_KEY, JSON.stringify(panelSize.value))
  }

  async function fetchSessions() {
    sessionsLoading.value = true
    error.value = null
    try {
      const resp = await api.GET('/api/v1/remy/sessions')
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
      } else {
        sessions.value = resp.data?.items ?? []
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : extractErrorMessage(e)
    } finally {
      sessionsLoading.value = false
    }
  }

  async function createSession() {
    error.value = null
    try {
      const resp = await api.POST('/api/v1/remy/sessions', {
        body: { name: null, provider: null, model: null, context_window_tokens: DEFAULT_CONTEXT_WINDOW_TOKENS } as any,
      })
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
        return null
      }
      const session = resp.data!
      if (Array.isArray(sessions.value)) sessions.value.unshift(session)
      activeSessionId.value = session.id
      messages.value = []
      return session
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : extractErrorMessage(e)
      return null
    }
  }

  async function loadSession(id: string) {
    loading.value = true
    error.value = null
    messages.value = []
    try {
      const resp = await api.GET('/api/v1/remy/sessions/{id}/messages', {
        params: { path: { id } },
      })
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
      } else {
        messages.value = resp.data ?? []
        activeSessionId.value = id
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : extractErrorMessage(e)
    } finally {
      loading.value = false
    }
  }

  async function deleteSession(id: string) {
    error.value = null
    try {
      const resp = await api.DELETE('/api/v1/remy/sessions/{id}', {
        params: { path: { id } },
      })
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
        return
      }
      sessions.value = Array.isArray(sessions.value) ? sessions.value.filter(s => s.id !== id) : []
      if (activeSessionId.value === id) {
        activeSessionId.value = null
        messages.value = []
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : extractErrorMessage(e)
    }
  }

  async function sendMessage(text: string) {
    if (!activeSessionId.value) return
    messages.value.push(createMessage('user', text, { session_id: activeSessionId.value }))
    isStreaming.value = true
  }

  function setPanelState(state: 'closed' | 'floating' | 'docked' | 'maximised') {
    panelState.value = state
  }

  function updatePosition(pos: { x: number; y: number }) {
    panelPosition.value = {
      x: Math.max(8, Math.min(pos.x, window.innerWidth - 340)),
      y: Math.max(8, Math.min(pos.y, window.innerHeight - 100)),
    }
    persistPosition()
  }

  function updateSize(size: { width: number; height: number }) {
    panelSize.value = {
      width: Math.max(100, Math.min(size.width, window.innerWidth - 16)),
      height: Math.max(100, Math.min(size.height, window.innerHeight - 40)),
    }
    persistSize()
  }

  function setPageContext(ctx: PageContext) {
    pageContext.value = ctx
  }

  function removeLastUserMessage() {
    const lastIdx = messages.value.length - 1
    if (lastIdx >= 0 && messages.value[lastIdx].role === 'user') {
      messages.value.splice(lastIdx, 1)
    }
  }

  function setPendingPermission(req: PermissionRequest | null) {
    pendingPermission.value = req
  }

  async function approvePermission(requestId: string, action: 'approve' | 'reject' | 'approve_for_session') {
    const headers = getAuthHeaders()
    try {
      const resp = await fetch(`/api/v1/remy/sessions/${activeSessionId.value}/permission-response`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...headers,
        },
        body: JSON.stringify({ request_id: requestId, action }),
      })
      if (!resp.ok) {
        console.warn('[RemyStore] Permission response failed', resp.status)
      }
      pendingPermission.value = null
    } catch (e) {
      console.warn('[RemyStore] Permission response error', e)
      pendingPermission.value = null
    }
  }

  async function resetSessionPermissions() {
    if (!activeSessionId.value) return
    const headers = getAuthHeaders()
    try {
      const resp = await fetch(`/api/v1/remy/sessions/${activeSessionId.value}/reset-permissions`, {
        method: 'POST',
        headers: { ...headers },
      })
      if (!resp.ok) {
        console.warn('[RemyStore] Reset permissions failed', resp.status)
      }
    } catch (e) {
      console.warn('[RemyStore] Reset permissions error', e)
    }
  }

  function appendSystemMessage(content: string) {
    messages.value.push(createMessage('summary', content, {
      session_id: activeSessionId.value ?? '',
    }))
  }

  function appendTurnSeparator(label: string) {
    messages.value.push(createMessage('summary', label, {
      session_id: activeSessionId.value ?? '',
    }))
  }

  function appendToken(text: string) {
    const lastMsg = messages.value[messages.value.length - 1]
    if (lastMsg && lastMsg.role === 'assistant') {
      lastMsg.content = (lastMsg.content ?? '') + text
    } else {
      messages.value.push(createMessage('assistant', text, {
        session_id: activeSessionId.value ?? '',
      }))
    }
  }

  function appendToolCall(tc: { tool_call_id: string; tool_name: string; success: boolean; result?: unknown; error?: string }) {
    const summary = tc.success
      ? `Tool: ${tc.tool_name} — completed`
      : `Tool: ${tc.tool_name} — failed: ${tc.error ?? 'unknown error'}`
    messages.value.push(createMessage('tool_result', summary, {
      session_id: activeSessionId.value ?? '',
      tool_results_json: { ...tc },
    }))
  }

  return {
    sessions,
    activeSessionId,
    messages,
    panelState,
    panelPosition,
    panelSize,
    isStreaming,
    pageContext,
    loading,
    error,
    sessionsLoading,
    pendingPermission,
    isExecutingUi,
    activeSession,
    sortedSessions,
    fetchSessions,
    createSession,
    loadSession,
    deleteSession,
    sendMessage,
    setPanelState,
    updatePosition,
    updateSize,
    setPageContext,
    appendToken,
    appendToolCall,
    removeLastUserMessage,
    setPendingPermission,
    approvePermission,
    resetSessionPermissions,
    appendSystemMessage,
    appendTurnSeparator,
    persistPosition,
    persistSize,
  }
})
