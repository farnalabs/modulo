import { ref, onUnmounted } from 'vue'
import { useRemyStore } from './useRemyStore'
import { getAccessToken } from '@/lib/api/client'
import { executeCommandBatch } from './useUiCommandExecutor'
import type { UiCommandResult } from './useUiCommandExecutor'

const MAX_BUFFER_SIZE = 1024 * 1024

export interface ToolCallEvent {
  tool_call_id: string
  tool_name: string
  success: boolean
  result?: unknown
  error?: string
}

export function useRemyStream() {
  const store = useRemyStore()
  const connected = ref(false)
  let abortController: AbortController | null = null

  async function connectStream(sessionId: string) {
    disconnectStream()
    abortController = new AbortController()
    connected.value = true
    store.isStreaming = true

    const session = store.sessions.find(s => s.id === sessionId)
    if (!session) {
      store.error = 'Session not found'
      store.isStreaming = false
      connected.value = false
      return
    }

    const lastMsg = store.messages[store.messages.length - 1]
    if (!lastMsg || !lastMsg.content) {
      store.removeLastUserMessage()
      store.isStreaming = false
      connected.value = false
      return
    }

    const token = getAccessToken()
    const pageCtx = store.pageContext
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

    try {
      const response = await fetch(`/api/v1/remy/sessions/${sessionId}/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          content: lastMsg.content,
          provider: session.provider,
          model: session.model,
          context_window_tokens: session.context_window_tokens,
          api_key: '',
          mcp_api_key: token || '',
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

      if (!response.ok || !response.body) {
        const errorDetail = response.status === 403 ? 'Access denied. Contact your admin.' : (response.statusText || 'Stream connection failed')
        store.error = errorDetail
        store.removeLastUserMessage()
        store.isStreaming = false
        connected.value = false
        return
      }

      reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let currentEvent = ''
      let streaming = true

      while (streaming) {
        const { done, value } = await reader.read()
        if (done) { streaming = false; continue }

        buffer += decoder.decode(value, { stream: true })
        if (buffer.length > MAX_BUFFER_SIZE) {
          store.error = 'Stream buffer overflow'
          store.isStreaming = false
          break
        }
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            const data = line.slice(6)
            try {
              const parsed = JSON.parse(data)
              if (currentEvent === 'token' && parsed.token) {
                store.appendToken(parsed.token)
              } else if (currentEvent === 'error') {
                store.error = parsed.detail ?? parsed.message ?? 'Stream error'
                streaming = false
              } else if (currentEvent === 'done') {
                streaming = false
              } else if (currentEvent === 'tool_call') {
                store.appendToolCall(parsed as ToolCallEvent)
              } else if (currentEvent === 'permission_request') {
                store.setPendingPermission(parsed)
              } else if (currentEvent === 'ui_command_batch') {
                const commands = parsed.commands ?? parsed
                store.isExecutingUi = true
                let results: UiCommandResult[]
                try {
                  results = await executeCommandBatch(commands)
                } catch (e) {
                  store.error = e instanceof Error ? e.message : 'UI command execution failed'
                  results = []
                  streaming = false
                }
                store.isExecutingUi = false
                try {
                  await fetch(`/api/v1/remy/sessions/${sessionId}/ui-command-results`, {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/json',
                      ...(token ? { Authorization: `Bearer ${token}` } : {}),
                    },
                    body: JSON.stringify({ results }),
                  })
                } catch {
                  store.error = 'Failed to submit UI command results'
                  streaming = false
                }
                store.isExecutingUi = false
                store.isStreaming = true
              } else if (currentEvent === 'turn_separator') {
                store.appendTurnSeparator(parsed.label ?? '---')
              } else if (currentEvent === 'abort_summary') {
                store.appendSystemMessage(parsed.summary ?? 'Action cancelled by user.')
                streaming = false
              } else if (currentEvent === 'ping') {
                // Keepalive — ignore
              }
            } catch {
              if (currentEvent === 'token' && data.trim()) {
                store.appendToken(data)
              }
            }
          }
        }
      }
    } catch (e: unknown) {
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
