import { ref, onUnmounted } from 'vue'
import { useRemyStore } from './useRemyStore'
import { getAccessToken } from '@/lib/api/client'

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
      store.isStreaming = false
      connected.value = false
      return
    }

    const token = getAccessToken()

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
        }),
        signal: abortController.signal,
      })

      if (!response.ok || !response.body) {
        store.isStreaming = false
        connected.value = false
        store.error = response.statusText || 'Stream connection failed'
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let currentEvent = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            const data = line.slice(6)
            if (data === '[DONE]') {
              store.isStreaming = false
              continue
            }
            try {
              const parsed = JSON.parse(data)
              if (currentEvent === 'token' && parsed.token) {
                store.appendToken(parsed.token)
              } else if (currentEvent === 'error' || (parsed.detail)) {
                store.error = parsed.detail ?? parsed.message ?? 'Stream error'
                store.isStreaming = false
              } else if (currentEvent === 'done' && parsed.message_id) {
                store.isStreaming = false
              }
            } catch {
              if (data.trim()) {
                store.appendToken(data)
              }
            }
          }
        }
      }
    } catch (e: unknown) {
      if (e instanceof Error && e.name === 'AbortError') return
      store.error = e instanceof Error ? e.message : 'Stream disconnected'
    } finally {
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
