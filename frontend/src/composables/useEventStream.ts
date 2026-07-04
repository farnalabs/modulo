import { ref, onMounted, onUnmounted } from 'vue'
import type { EventBusEvent } from '@/types/events'
import { getHandlers } from '@/stores/syncRegistry'
import { getAccessToken } from '@/lib/api/client'

export type EventHandler = (event: EventBusEvent) => void

const SSE_URL = '/api/v1/events'

const connected = ref(false)
let abortController: AbortController | null = null
const handlers = new Map<string, Set<EventHandler>>()
let reconnectAttempts = 0
const MAX_RECONNECT_ATTEMPTS = 10
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

function connect(): void {
  if (abortController) return
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  reconnectAttempts = 0
  doConnect()
}

function scheduleReconnect(): void {
  connected.value = false
  reconnectAttempts++
  if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
    console.error('[EventBus] Max reconnect attempts reached')
    abortController = null
    return
  }
  const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 30000)
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    doConnect()
  }, delay)
}

async function doConnect(): Promise<void> {
  cleanup()
  abortController = new AbortController()
  const token = getAccessToken()
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

  try {
    const response = await fetch(SSE_URL, {
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      signal: abortController.signal,
    })

    if (!response.ok || !response.body) {
      scheduleReconnect()
      return
    }

    connected.value = true
    reconnectAttempts = 0

    reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let currentEvent = ''

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim()
        } else if (line.startsWith('data: ')) {
          const dataLine = line.slice(6)
          try {
            const data = JSON.parse(dataLine)
            if (currentEvent === 'resource_changed') {
              const typeHandlers = handlers.get(data.type)
              if (typeHandlers) {
                for (const handler of typeHandlers) {
                  try { handler(data) } catch (e) {
                    console.error('[EventBus] Handler error', String(e))
                  }
                }
              }
              try { dispatchToStore(data) } catch (e) {
                console.error('[EventBus] dispatchToStore error', String(e))
              }
            }
          } catch {
            // Ignore parse errors on SSE data lines
          }
          currentEvent = ''
        }
      }
    }
  } catch (e: unknown) {
    if (e instanceof Error && e.name === 'AbortError') return
    scheduleReconnect()
    return
  } finally {
    reader?.cancel().catch(() => {})
    connected.value = false
  }

  scheduleReconnect()
}

function cleanup(): void {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (abortController) {
    abortController.abort()
    abortController = null
  }
  connected.value = false
}

function disconnect(): void {
  cleanup()
  clearAllHandlers()
}

function clearAllHandlers(): void {
  handlers.clear()
}

function dispatchToStore(event: EventBusEvent): void {
  const storeHandlers = getHandlers(event.type)
  for (const handler of storeHandlers) {
    handler(event)
  }
}

export const eventBus = {
  get connected() { return connected.value },
  subscribe(resourceType: string, handler: EventHandler): () => void {
    if (!handlers.has(resourceType)) handlers.set(resourceType, new Set())
    handlers.get(resourceType)!.add(handler)
    if (!abortController) connect()
    return () => { eventBus.unsubscribe(resourceType, handler) }
  },
  unsubscribe(resourceType: string, handler: EventHandler): void {
    const typeHandlers = handlers.get(resourceType)
    if (typeHandlers) {
      typeHandlers.delete(handler)
      if (typeHandlers.size === 0) handlers.delete(resourceType)
    }
    if (totalHandlerCount() <= 0) disconnect()
  },
  reconnect(): void {
    cleanup()
    reconnectAttempts = 0
    doConnect()
  },
}

function totalHandlerCount(): number {
  let count = 0
  for (const typeHandlers of handlers.values()) count += typeHandlers.size
  return count
}

export function useEventStream(options?: { resourceType?: string; onEvent?: EventHandler }) {
  if (options?.resourceType && options?.onEvent) {
    const resourceType: string = options.resourceType
    const onEvent: EventHandler = options.onEvent
    let unsub: (() => void) | null = null
    onMounted(() => {
      unsub = eventBus.subscribe(resourceType, onEvent)
    })
    onUnmounted(() => {
      unsub?.()
    })
  }
  return { connected }
}

export { dispatchToStore }

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    cleanup()
    clearAllHandlers()
  })
}
