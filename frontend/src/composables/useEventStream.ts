import { ref, onMounted, onUnmounted } from 'vue'
import type { EventBusEvent } from '@/types/events'
import { getHandlers } from '@/stores/syncRegistry'

export type EventHandler = (event: EventBusEvent) => void

const SSE_URL = '/api/v1/events'

const connected = ref(false)
let eventSource: EventSource | null = null
const handlers = new Map<string, Set<EventHandler>>()
let reconnectAttempts = 0
const MAX_RECONNECT_ATTEMPTS = 10
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

function connect(): void {
  if (eventSource) return
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  reconnectAttempts = 0
  doConnect()
}

function doConnect(): void {
  if (eventSource) eventSource.close()
  eventSource = new EventSource(SSE_URL, { withCredentials: true })
  eventSource.onopen = () => {
    connected.value = true
    reconnectAttempts = 0
  }
  eventSource.onmessage = (event: MessageEvent) => {
    let data: EventBusEvent
    try {
      data = JSON.parse(event.data)
    } catch (e) {
      console.warn('[EventBus] Failed to parse event', e instanceof SyntaxError ? e.message : String(e))
      return
    }
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
  eventSource.onerror = () => {
    connected.value = false
    reconnectAttempts++
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
      console.error('[EventBus] Max reconnect attempts reached')
      return
    }
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 30000)
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      doConnect()
    }, delay)
  }
}

function cleanup(): void {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (eventSource) {
    eventSource.close()
    eventSource = null
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
    if (!eventSource) connect()
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

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    cleanup()
    clearAllHandlers()
  })
}
