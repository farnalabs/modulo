import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, getAccessToken } from '@/lib/api/client'
import type { ChatSession, ChatMessage, PageContext } from '@/types/remy'

export interface PermissionRequest {
  request_id: string
  tools: Array<{ name: string; args: Record<string, unknown> }>
}

const POSITION_KEY = 'remy_panel_position'
const SIZE_KEY = 'remy_panel_size'

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
  } catch { /* ignore */ }
  const defaultX = Math.max(8, window.innerWidth - 460)
  return { x: defaultX, y: 80 }
}

function loadSize(): { width: number; height: number } {
  try {
    const raw = localStorage.getItem(SIZE_KEY)
    if (raw) return JSON.parse(raw)
  } catch { /* ignore */ }
  const defaultWidth = Math.min(440, window.innerWidth - 16)
  const defaultHeight = Math.min(600, window.innerHeight - 120)
  return { width: defaultWidth, height: defaultHeight }
}

function extractErrorMessage(err: unknown): string {
  if (typeof err === 'string') return err
  if (err && typeof err === 'object') {
    const obj = err as Record<string, unknown>
    if (typeof obj.detail === 'string') return obj.detail
    if (typeof obj.message === 'string') return obj.message
  }
  return String(err)
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
    sessions.value.find(s => s.id === activeSessionId.value) ?? null,
  )

  const sortedSessions = computed(() =>
    [...sessions.value].sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    ),
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
      const { data, error: err } = await (api as any).GET('/api/v1/remy/sessions')
      if (err) {
        error.value = extractErrorMessage(err)
      } else {
        sessions.value = (data as any) ?? []
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
      const { data, error: err } = await (api as any).POST('/api/v1/remy/sessions', {
        body: { name: null, provider: 'anthropic', model: 'claude-sonnet-4-20250514', context_window_tokens: 200000 },
      })
      if (err) throw new Error(extractErrorMessage(err))
      const session = data as ChatSession
      sessions.value.unshift(session)
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
    activeSessionId.value = id
    messages.value = []
    try {
      const { data, error: err } = await (api as any).GET('/api/v1/remy/sessions/{id}/messages', {
        params: { path: { id } },
      })
      if (err) {
        error.value = extractErrorMessage(err)
      } else {
        messages.value = (data as any) ?? []
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
      const { error: err } = await (api as any).DELETE('/api/v1/remy/sessions/{id}', {
        params: { path: { id } },
      })
      if (err) throw new Error(extractErrorMessage(err))
      sessions.value = sessions.value.filter(s => s.id !== id)
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
    const userMsg: ChatMessage = {
      id: `msg-${Date.now()}`,
      session_id: activeSessionId.value,
      role: 'user',
      content: text,
      tool_calls_json: null,
      tool_results_json: null,
      token_count: null,
      parent_id: null,
      created_at: new Date().toISOString(),
    }
    messages.value.push(userMsg)
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
      width: Math.min(size.width, window.innerWidth - 16),
      height: Math.min(size.height, window.innerHeight - 40),
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
    const token = getAccessToken()
    try {
      await fetch(`/api/v1/remy/sessions/${activeSessionId.value}/permission-response`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ request_id: requestId, action }),
      })
    } catch {
      // Best effort — the stream will surface errors
    }
    pendingPermission.value = null
  }

  async function resetSessionPermissions() {
    if (!activeSessionId.value) return
    const token = getAccessToken()
    try {
      await fetch(`/api/v1/remy/sessions/${activeSessionId.value}/reset-permissions`, {
        method: 'POST',
        headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      })
    } catch {
      // Best effort
    }
  }

  function appendSystemMessage(content: string) {
    messages.value.push({
      id: `sys-${Date.now()}`,
      session_id: activeSessionId.value ?? '',
      role: 'summary',
      content,
      tool_calls_json: null,
      tool_results_json: null,
      token_count: null,
      parent_id: null,
      created_at: new Date().toISOString(),
    })
  }

  function appendTurnSeparator(label: string) {
    messages.value.push({
      id: `sep-${Date.now()}`,
      session_id: activeSessionId.value ?? '',
      role: 'summary',
      content: label,
      tool_calls_json: null,
      tool_results_json: null,
      token_count: null,
      parent_id: null,
      created_at: new Date().toISOString(),
    })
  }

  function appendToken(text: string) {
    const lastMsg = messages.value[messages.value.length - 1]
    if (lastMsg && lastMsg.role === 'assistant') {
      lastMsg.content = (lastMsg.content ?? '') + text
    } else {
      messages.value.push({
        id: `stream-${Date.now()}`,
        session_id: activeSessionId.value ?? '',
        role: 'assistant',
        content: text,
        tool_calls_json: null,
        tool_results_json: null,
        token_count: null,
        parent_id: null,
        created_at: new Date().toISOString(),
      })
    }
  }

  function appendToolCall(tc: { tool_call_id: string; tool_name: string; success: boolean; result?: unknown; error?: string }) {
    const summary = tc.success
      ? `Tool: ${tc.tool_name} — completed`
      : `Tool: ${tc.tool_name} — failed: ${tc.error ?? 'unknown error'}`
    messages.value.push({
      id: `tool-${Date.now()}-${tc.tool_call_id}`,
      session_id: activeSessionId.value ?? '',
      role: 'tool_result',
      content: summary,
      tool_calls_json: null,
      tool_results_json: null,
      token_count: null,
      parent_id: null,
      created_at: new Date().toISOString(),
    })
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
