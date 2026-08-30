import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useStorage } from '@vueuse/core'
import { api, type components } from '@/lib/api/client'
import { formatApiError } from '@/lib/api/formatError'
import { toDate } from '@/lib/formatDate'
import { pauseUiCommands, resumeUiCommands } from './useUiCommandExecutor'
import type { ChatSession, ChatMessage, PageContext, ToolResult } from '@/types/remy'

export interface PermissionRequest {
  request_id: string
  tools: Array<{ name: string; args: Record<string, unknown>; nogo?: boolean }>
}

const DEFAULT_CONTEXT_WINDOW_TOKENS = 200000

const NARROW_VIEWPORT_PX = 640

function extractErrorMessage(err: unknown): string {
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
  const activeSessionId = useStorage<string | null>('remy-active-session', null)
  const messages = ref<ChatMessage[]>([])
  const panelState = useStorage<'closed' | 'floating' | 'docked' | 'maximised'>('remy-panel-state', 'docked')
  const panelPosition = useStorage('remy-panel-position', { x: Math.max(8, window.innerWidth - 460), y: 80 })
  const panelSize = useStorage('remy-panel-size', { width: Math.min(440, window.innerWidth - 16), height: Math.min(600, window.innerHeight - 120) })
  const isStreaming = ref(false)
  const pageContext = ref<PageContext>({ route: '', params: {}, entities: [] })
  const loading = ref(false)
  const error = ref<string | null>(null)
  const sessionsLoading = ref(false)
  const pendingPermission = ref<PermissionRequest | null>(null)
  const isExecutingUi = ref(false)
  const isPaused = ref(false)
  const requestRename = ref(0)
  const skillsVersion = ref(0)

  function triggerRename() {
    requestRename.value++
  }

  function signalSkillsChanged() {
    skillsVersion.value++
  }

  const activeSession = computed(() =>
    Array.isArray(sessions.value) ? sessions.value.find(s => s.id === activeSessionId.value) ?? null : null,
  )

  const sortedSessions = computed(() =>
    Array.isArray(sessions.value)
      ? [...sessions.value].sort((a, b) => {
          const ta = toDate(a.updated_at)
          const tb = toDate(b.updated_at)
          if (!ta || !tb) return 0
          return tb.getTime() - ta.getTime()
        })
      : [],
  )

  async function fetchSessions() {
    sessionsLoading.value = true
    error.value = null
    try {
      const resp = await api.GET('/api/v1/remy/sessions')
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
      } else {
        const payload = resp.data as unknown as { items?: ChatSession[] } | undefined
        sessions.value = payload?.items ?? []
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
        body: { name: null, provider: null, model: null, context_window_tokens: DEFAULT_CONTEXT_WINDOW_TOKENS } as unknown as components['schemas']['CreateSessionRequest'],
      })
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
        return null
      }
      const session = resp.data as unknown as ChatSession
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
      const resp = await api.GET('/api/v1/remy/sessions/{session_id}/messages', {
        params: { path: { session_id: id } },
      })
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
      } else {
        messages.value = (resp.data as unknown as { items?: ChatMessage[] } | undefined)?.items ?? []
        activeSessionId.value = id
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : extractErrorMessage(e)
    } finally {
      loading.value = false
    }
  }

  async function renameSession(id: string, name: string): Promise<boolean> {
    error.value = null
    try {
      const resp = await api.PATCH('/api/v1/remy/sessions/{session_id}', {
        params: { path: { session_id: id } },
        body: { name },
      })
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
        return false
      }
      const updated = resp.data as unknown as ChatSession
      if (Array.isArray(sessions.value)) {
        const idx = sessions.value.findIndex(s => s.id === id)
        if (idx >= 0) sessions.value[idx] = updated
      }
      return true
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : extractErrorMessage(e)
      return false
    }
  }

  async function deleteSession(id: string) {
    error.value = null
    try {
      const resp = await api.DELETE('/api/v1/remy/sessions/{session_id}', {
        params: { path: { session_id: id } },
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

  function collapseIfNarrow() {
    if (window.innerWidth < NARROW_VIEWPORT_PX && panelState.value !== 'closed') {
      panelState.value = 'closed'
    }
  }

  function disposeResponsive() {
    window.removeEventListener('resize', collapseIfNarrow)
  }

  function reclampPosition() {
    panelPosition.value = {
      x: Math.max(8, Math.min(panelPosition.value.x, window.innerWidth - 340)),
      y: Math.max(8, Math.min(panelPosition.value.y, window.innerHeight - 100)),
    }
  }

  function updatePosition(pos: { x: number; y: number }) {
    panelPosition.value = {
      x: Math.max(8, Math.min(pos.x, window.innerWidth - 340)),
      y: Math.max(8, Math.min(pos.y, window.innerHeight - 100)),
    }
  }

  function updateSize(size: { width: number; height: number }) {
    panelSize.value = {
      width: Math.max(100, Math.min(size.width, window.innerWidth - 16)),
      height: Math.max(100, Math.min(size.height, window.innerHeight - 40)),
    }
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
    if (!activeSessionId.value) {
      error.value = "Cannot approve permission: no active session"
      return;
    }
    error.value = null
    try {
      const resp = await api.POST('/api/v1/remy/sessions/{session_id}/permission-response', {
        params: { path: { session_id: activeSessionId.value } },
        body: { request_id: requestId, action },
      })
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
      }
      pendingPermission.value = null
    } catch (e) {
      error.value = extractErrorMessage(e)
      pendingPermission.value = null
    }
  }

  async function resetSessionPermissions() {
    if (!activeSessionId.value) return
    error.value = null
    try {
      const resp = await api.POST('/api/v1/remy/sessions/{session_id}/reset-permissions', {
        params: { path: { session_id: activeSessionId.value } },
      })
      if (resp.error) {
        error.value = extractErrorMessage(resp.error)
      }
    } catch (e) {
      error.value = extractErrorMessage(e)
    }
  }

  function pauseRemy() {
    isPaused.value = true
    pauseUiCommands()
  }

  function resumeRemy() {
    isPaused.value = false
    resumeUiCommands()
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

  function appendToolCall(tc: ToolResult) {
    const summary = tc.success
      ? `Tool: ${tc.tool_name} — completed`
      : `Tool: ${tc.tool_name} — failed: ${tc.error ?? 'unknown error'}`
    messages.value.push(createMessage('tool_result', summary, {
      session_id: activeSessionId.value ?? '',
      tool_results_json: { ...tc },
    }))
  }

  collapseIfNarrow()
  window.addEventListener('resize', collapseIfNarrow)

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
    isPaused,
    requestRename,
    triggerRename,
    skillsVersion,
    signalSkillsChanged,
    activeSession,
    sortedSessions,
    fetchSessions,
    createSession,
    loadSession,
    renameSession,
    deleteSession,
    sendMessage,
    setPanelState,
    collapseIfNarrow,
    disposeResponsive,
    reclampPosition,
    updatePosition,
    updateSize,
    setPageContext,
    appendToken,
    appendToolCall,
    removeLastUserMessage,
    setPendingPermission,
    approvePermission,
    resetSessionPermissions,
    pauseRemy,
    resumeRemy,
    appendSystemMessage,
    appendTurnSeparator,
  }
})
