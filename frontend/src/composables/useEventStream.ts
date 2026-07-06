import { ref, onMounted, onUnmounted } from 'vue'
import type { EventBusEvent } from '@/types/events'
import { getHandlers } from '@/stores/syncRegistry'
import { getAuthHeaders } from '@/lib/api/client'
import { parseSSEStream } from '@/lib/sse'

export type EventHandler = (event: EventBusEvent) => void

const SSE_URL = '/api/v1/events'

const connected = ref(false)
let abortController: AbortController | null = null
const handlers = new Map<string, Set<EventHandler>>()
let reconnectAttempts = 0
const MAX_RECONNECT_ATTEMPTS = 10
const FETCH_TIMEOUT_MS = 30000
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

function connect(): void {
  if (abortController) return
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
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
  const timeoutId = setTimeout(() => abortController?.abort(), FETCH_TIMEOUT_MS)
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

  try {
    const response = await fetch(SSE_URL, {
      headers: { ...getAuthHeaders() },
      signal: abortController.signal,
    })
    clearTimeout(timeoutId)

    if (!response.ok || !response.body) {
      scheduleReconnect()
      return
    }

    connected.value = true
    reconnectAttempts = 0

    reader = response.body.getReader()

    for await (const { event, data } of parseSSEStream(reader)) {
      if (event === 'resource_changed') {
        try {
          const parsed = JSON.parse(data)
          const typeHandlers = handlers.get(parsed.type)
          if (typeHandlers) {
            for (const handler of typeHandlers) {
              try { handler(parsed) } catch (e) {
                console.error('[EventBus] Handler error', e)
              }
            }
          }
          dispatchToStore(parsed)
        } catch {
          console.warn('[EventBus] Failed to parse SSE data')
        }
      }
    }
  } catch (e: unknown) {
    clearTimeout(timeoutId)
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
    if (handlers.size === 0) disconnect()
  },
  reconnect(): void {
    cleanup()
    reconnectAttempts = 0
    doConnect()
  },
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
