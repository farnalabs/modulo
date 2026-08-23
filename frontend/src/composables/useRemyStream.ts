import { getCurrentInstance, ref, onUnmounted } from 'vue'
import { useRemyStore } from './useRemyStore'
import { usePlanStore } from '../stores/planStore'
import { getAuthHeaders } from '@/lib/api/client'
import { parseSSEStream } from '@/lib/sse'
import { executeCommandBatch, isPaused as isExecutorPaused } from './useUiCommandExecutor'

export interface ToolCallEvent {
  tool_call_id: string
  tool_name: string
  success: boolean
  result?: unknown
  error?: string
}

export interface StreamOptions {
  excludeUiTools?: boolean
}

const FETCH_TIMEOUT_MS = 30000

export function useRemyStream() {
  const store = useRemyStore()
  const connected = ref(false)
  let abortController: AbortController | null = null
  let streamId = 0
  let activeStream: Promise<void> | null = null

  async function connectStream(sessionId: string, options: StreamOptions = {}) {
    if (connected.value && store.activeSessionId === sessionId) return
    await disconnectStream()
    const seq = ++streamId
    const controller = new AbortController()
    abortController = controller
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS)
    connected.value = true
    store.isStreaming = true

    const run = runStream(sessionId, options, seq, controller, timeoutId)
    activeStream = run
    return run
  }

  async function runStream(
    sessionId: string,
    options: StreamOptions,
    seq: number,
    controller: AbortController,
    timeoutId: ReturnType<typeof setTimeout>,
  ): Promise<void> {
    const session = store.sessions.find(s => s.id === sessionId)
    if (!session) {
      store.error = 'Session not found'
      clearTimeout(timeoutId)
      if (seq === streamId) {
        store.isStreaming = false
        connected.value = false
      }
      return
    }

    const lastMsg = store.messages[store.messages.length - 1]
    if (!lastMsg || !lastMsg.content) {
      store.removeLastUserMessage()
      clearTimeout(timeoutId)
      if (seq === streamId) {
        store.isStreaming = false
        connected.value = false
      }
      return
    }

    const headers = getAuthHeaders()
    const pageCtx = store.pageContext
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

    try {
      const response = await fetch(`/api/v1/remy/sessions/${sessionId}/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...headers,
        },
        body: JSON.stringify({
          content: lastMsg.content,
          provider: session.provider,
          model: session.model,
          context_window_tokens: session.context_window_tokens,
          api_key: '',
          mcp_api_key: headers.Authorization?.replace('Bearer ', '') || '',
          page_context: options.excludeUiTools
            ? undefined
            : (() => {
                if (!pageCtx.route) return undefined
                let ctx = `Page: ${pageCtx.route}`
                if (pageCtx.params.id) ctx += ` / ${pageCtx.params.id}`
                if (pageCtx.entities.length) ctx += `\nEntities: ${pageCtx.entities.join(', ')}`
                return ctx
              })(),
          exclude_ui_tools: options.excludeUiTools || undefined,
        }),
        signal: controller.signal,
      })
      clearTimeout(timeoutId)

      if (!response.ok || !response.body) {
        const errorDetail = response.status === 403 ? 'Access denied. Contact your admin.' : (response.statusText || 'Stream connection failed')
        store.error = errorDetail
        store.removeLastUserMessage()
        return
      }

      reader = response.body.getReader()

      for await (const { event: currentEvent, data } of parseSSEStream(reader)) {
        try {
          const parsed = JSON.parse(data)
          if (currentEvent === 'token' && parsed.token) {
            store.appendToken(parsed.token)
          } else if (currentEvent === 'error') {
            store.error = parsed.detail ?? parsed.message ?? 'Stream error'
            break
          } else if (currentEvent === 'done') {
            break
          } else if (currentEvent === 'tool_call') {
            store.appendToolCall(parsed as ToolCallEvent)
          } else if (currentEvent === 'permission_request') {
            store.setPendingPermission(parsed)
          } else if (currentEvent === 'ui_command_batch') {
            const planStore = usePlanStore()
            // Belt-and-braces: remy-only mode never executes UI command batches,
            // and never submits them even if the server (or a text-mode backend
            // path) somehow emits one.
            if (options.excludeUiTools || !planStore.featureEnabled('remy_ui_driving')) {
              console.warn('[RemyStream] UI driving disabled — skipping command batch')
              const body = JSON.stringify({ results: [] })
              await fetch(`/api/v1/remy/sessions/${sessionId}/ui-command-results`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...headers },
                body,
              })
              continue
            }
            const commands = parsed.commands ?? parsed
            store.isExecutingUi = true
            store.isPaused = false
            try {
              const results = await executeCommandBatch(commands)
              store.isExecutingUi = false
              const streamSignal = controller?.signal
              const pauseDeadline = Date.now() + 60000
              while (isExecutorPaused()) {
                if (Date.now() > pauseDeadline) break
                if (streamSignal?.aborted) break
                await new Promise(r => setTimeout(r, 200))
              }
              const body = JSON.stringify({ results })
              const maxRetries = 3
              for (let retries = 0; retries < maxRetries; retries++) {
                const resp = await fetch(`/api/v1/remy/sessions/${sessionId}/ui-command-results`, {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    ...headers,
                  },
                  body,
                })
                if (resp.ok) break
                if (retries >= maxRetries - 1) {
                  store.error = `Failed to submit UI command results (${resp.status})`
                } else {
                  await new Promise(r => setTimeout(r, 500 * (retries + 1)))
                }
              }
            } catch (e) {
              store.error = e instanceof Error ? e.message : 'UI command execution failed'
              store.isExecutingUi = false
              break
            }
          } else if (currentEvent === 'turn_separator') {
            store.appendTurnSeparator(parsed.label ?? '---')
          } else if (currentEvent === 'abort_summary') {
            store.appendSystemMessage(parsed.summary ?? 'Action cancelled by user.')
            break
          }
          // ping — keepalive, ignore
        } catch {
          if (currentEvent === 'token' && data.trim()) {
            store.appendToken(data)
          }
        }
      }
    } catch (e: unknown) {
      clearTimeout(timeoutId)
      if (e instanceof Error && e.name === 'AbortError') return
      store.error = e instanceof Error ? e.message : 'Stream disconnected'
      store.removeLastUserMessage()
    } finally {
      reader?.cancel().catch(() => {})
      if (seq === streamId) {
        store.isStreaming = false
        connected.value = false
      }
    }
  }

  async function disconnectStream() {
    const pendingId = streamId
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    const pending = activeStream
    activeStream = null
    if (pending) {
      try {
        await pending
      } catch (e) {
        // runStream handles its own errors
        console.warn('[RemyStream] disconnect error while awaiting stream', e)
      }
    }
    if (streamId === pendingId) {
      connected.value = false
      store.isStreaming = false
    }
  }

  if (getCurrentInstance()) {
    onUnmounted(() => {
      disconnectStream()
    })
  }

  return { connected, connectStream, disconnectStream }
}
