import { ref, onUnmounted } from 'vue'
import { useRemyStore } from './useRemyStore'
import { getAuthHeaders } from '@/lib/api/client'
import { parseSSEStream } from '@/lib/sse'
import { executeCommandBatch } from './useUiCommandExecutor'
import type { UiCommandResult } from './useUiCommandExecutor'

export interface ToolCallEvent {
  tool_call_id: string
  tool_name: string
  success: boolean
  result?: unknown
  error?: string
}

const FETCH_TIMEOUT_MS = 30000

export function useRemyStream() {
  const store = useRemyStore()
  const connected = ref(false)
  let abortController: AbortController | null = null

  async function connectStream(sessionId: string) {
    disconnectStream()
    abortController = new AbortController()
    const timeoutId = setTimeout(() => abortController?.abort(), FETCH_TIMEOUT_MS)
    connected.value = true
    store.isStreaming = true

    const session = store.sessions.find(s => s.id === sessionId)
    if (!session) {
      store.error = 'Session not found'
      store.isStreaming = false
      connected.value = false
      clearTimeout(timeoutId)
      return
    }

    const lastMsg = store.messages[store.messages.length - 1]
    if (!lastMsg || !lastMsg.content) {
      store.removeLastUserMessage()
      store.isStreaming = false
      connected.value = false
      clearTimeout(timeoutId)
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
          page_context: (() => {
            if (!pageCtx.route) return undefined
            let ctx = `Page: ${pageCtx.route}`
            if (pageCtx.params.id) ctx += ` / ${pageCtx.params.id}`
            if (pageCtx.entities.length) ctx += `\nEntities: ${pageCtx.entities.join(', ')}`
            return ctx
          })(),
        }),
        signal: abortController.signal,
      })
      clearTimeout(timeoutId)

      if (!response.ok || !response.body) {
        const errorDetail = response.status === 403 ? 'Access denied. Contact your admin.' : (response.statusText || 'Stream connection failed')
        store.error = errorDetail
        store.removeLastUserMessage()
        store.isStreaming = false
        connected.value = false
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
            const commands = parsed.commands ?? parsed
            store.isExecutingUi = true
            try {
              const results = await executeCommandBatch(commands)
              store.isExecutingUi = false
              const body = JSON.stringify({ results })
              let retries = 0
              const maxRetries = 3
              while (true) {
                const resp = await fetch(`/api/v1/remy/sessions/${sessionId}/ui-command-results`, {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    ...headers,
                  },
                  body,
                })
                if (resp.ok) break
                retries++
                if (retries >= maxRetries) {
                  store.error = `Failed to submit UI command results (${resp.status})`
                  break
                }
                await new Promise(r => setTimeout(r, 500 * retries))
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
      store.isStreaming = false
      connected.value = false
    }
  }

  function disconnectStream() {
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    connected.value = false
    store.isStreaming = false
  }

  onUnmounted(() => {
    disconnectStream()
  })

  return { connected, connectStream, disconnectStream }
}
