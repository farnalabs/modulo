import { ref, onMounted, onUnmounted } from 'vue'
import type { EventBusEvent } from '@/types/events'
import { getHandlers } from '@/stores/syncRegistry'

export type EventHandler = (event: EventBusEvent) => void

const TOKEN_KEY = 'modulo_access_token'
const SSE_URL = '/api/v1/events'

const connected = ref(false)
let eventSource: EventSource | null = null
const handlers = new Map<string, Set<EventHandler>>()

function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

function connect(): void {
  if (eventSource) return
  const token = getToken()
  if (!token) return
  eventSource = new EventSource(`${SSE_URL}?token=${encodeURIComponent(token)}`)
  eventSource.withCredentials = true
  eventSource.onopen = () => { connected.value = true }
  eventSource.onmessage = (event: MessageEvent) => {
    try {
      const data: EventBusEvent = JSON.parse(event.data)
      const typeHandlers = handlers.get(data.type)
      if (typeHandlers) {
        for (const handler of typeHandlers) {
          handler(data)
        }
      }
      dispatchToStore(data)
    } catch (e) {
      console.warn('[EventBus] Failed to parse event', e instanceof SyntaxError ? e.message : String(e), event.data.slice(0, 200))
    }
  }
  eventSource.onerror = () => {
    connected.value = false
  }
}

function disconnect(): void {
  if (eventSource) {
    eventSource.close()
    eventSource = null
    connected.value = false
  }
}

export function dispatchToStore(event: EventBusEvent): void {
  const storeHandlers = getHandlers(event.type)
  for (const handler of storeHandlers) {
    handler(event)
  }
}

export const eventBus = {
  get connected() { return connected },
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
}

function totalHandlerCount(): number {
  let count = 0
  for (const typeHandlers of handlers.values()) count += typeHandlers.size
  return count
}

export function useEventStream(options?: { resourceType?: string; onEvent?: EventHandler }) {
  if (options?.resourceType && options?.onEvent) {
    let unsub: (() => void) | null = null
    onMounted(() => {
      unsub = eventBus.subscribe(options.resourceType!, options.onEvent!)
    })
    onUnmounted(() => {
      unsub?.()
    })
  }
  return { connected }
}
