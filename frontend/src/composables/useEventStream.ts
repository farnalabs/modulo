import { ref, onMounted, onUnmounted } from 'vue'
import type { EventBusEvent } from '@/types/events'
import { getHandlers } from '@/stores/syncRegistry'
import { getAuthHeaders } from '@/lib/api/client'
import { parseSSEStream } from '@/lib/sse'

export type EventHandler = (event: EventBusEvent) => void

/** Stream lifecycle state (FAR-250 classified reconnect). */
export type EventStreamState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'auth_failed'

const SSE_URL = '/api/v1/events'

const connected = ref(false)
const connectionState = ref<EventStreamState>('idle')
let abortController: AbortController | null = null
const handlers = new Map<string, Set<EventHandler>>()
let reconnectAttempts = 0
const FETCH_TIMEOUT_MS = 30000
const BACKOFF_BASE_MS = 1000
const BACKOFF_MAX_MS = 30000
/** Debounce window for post-reconnect REST backfill (ticket: 1-2s). */
const RECONNECT_BACKFILL_DEBOUNCE_MS = 1500
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let backfillTimer: ReturnType<typeof setTimeout> | null = null
let _disconnecting = false
/** True once this tab has had a successful stream (distinguishes reconnects). */
let _everConnected = false

// Post-reconnect backfill subscribers (unread count / dropdown refetch).
const backfillSubscribers = new Set<() => void>()

function scheduleReconnectBackfill(): void {
  if (backfillTimer) {
    clearTimeout(backfillTimer)
  }
  backfillTimer = setTimeout(() => {
    backfillTimer = null
    for (const cb of backfillSubscribers) {
      try {
        cb()
      } catch (e) {
        console.error('[EventBus] Reconnect backfill handler error', e)
      }
    }
  }, RECONNECT_BACKFILL_DEBOUNCE_MS)
}

/**
 * Classify a failed HTTP response (FAR-250):
 * - 4xx (401/403/429 connection cap/any client error) -> STOP retrying and
 *   surface the reconnect banner; a fresh token / freed cap is required.
 * - 5xx and everything else -> retry forever with capped backoff + jitter.
 */
function classifyHttpFailure(status: number | undefined): 'auth' | 'retry' {
  if (typeof status === 'number' && status >= 400 && status < 500) {
    return 'auth'
  }
  return 'retry'
}

/**
 * CSPRNG-backed fraction in [0, 1) used for reconnect half-jitter. Sourced
 * from crypto.getRandomValues, not Math.random (Sonar S2245) — every
 * supported browser exposes WebCrypto, so there is deliberately no PRNG
 * fallback (same convention as utils/password.ts).
 */
function secureRandomFraction(): number {
  const buf = new Uint32Array(1)
  crypto.getRandomValues(buf)
  return buf[0] / 0x100000000
}

function scheduleReconnect(kind: 'auth' | 'retry'): void {
  connected.value = false
  if (kind === 'auth') {
    connectionState.value = 'auth_failed'
    abortController = null
    console.warn('[EventBus] Stopping stream: server rejected it (4xx). Restart with a fresh token.')
    return
  }
  connectionState.value = 'reconnecting'
  reconnectAttempts++
  // Capped exponential backoff with half-jitter; NEVER gives up (the old
  // 10-attempt cap was the bug — long outages must self-heal).
  const exp = Math.min(BACKOFF_BASE_MS * 2 ** Math.min(reconnectAttempts - 1, 10), BACKOFF_MAX_MS)
  const delay = exp / 2 + secureRandomFraction() * (exp / 2)
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    doConnect()
  }, delay)
}

function clearReconnectTimer(): void {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}

function connect(): void {
  if (abortController) return
  if (connectionState.value === 'auth_failed') return // banner restart required
  _disconnecting = false
  clearReconnectTimer()
  connectionState.value = 'connecting'
  doConnect()
}

function handleConnectError(timeoutId: ReturnType<typeof setTimeout>, e: unknown): void {
  clearTimeout(timeoutId)
  if (e instanceof Error && e.name === 'AbortError') {
    // Fetch timeout (30s) or explicit abort — a zombie/dead stream: retry.
    if (!_disconnecting) scheduleReconnect('retry')
    return
  }
  // Network-level failure (TypeError etc.): retry forever.
  scheduleReconnect('retry')
}

function dispatchToTypeHandlers(typeHandlers: Set<EventHandler>, parsed: EventBusEvent): void {
  for (const handler of typeHandlers) {
    try { handler(parsed) } catch (e) {
      console.error('[EventBus] Handler error', e)
    }
  }
}

function dispatchResourceEvent(parsed: EventBusEvent): void {
  const typeHandlers = handlers.get(parsed.type)
  if (typeHandlers) {
    dispatchToTypeHandlers(typeHandlers, parsed)
  }
  try { dispatchToStore(parsed) } catch (e) {
    console.error('[EventBus] dispatchToStore error', e)
  }
}

function handleResourceChanged(data: string): void {
  try {
    dispatchResourceEvent(JSON.parse(data) as EventBusEvent)
  } catch {
    console.warn('[EventBus] Failed to parse SSE data')
  }
}

async function doConnect(): Promise<void> {
  cleanup()
  abortController = new AbortController()
  // connect() blocks while auth_failed and reconnect() clears it, so doConnect
  // is only ever reached from a non-auth-stopped state.
  connectionState.value = 'connecting'
  const timeoutId = setTimeout(() => abortController?.abort(), FETCH_TIMEOUT_MS)
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

  try {
    const response = await fetch(SSE_URL, {
      headers: { ...getAuthHeaders() },
      signal: abortController.signal,
    })
    clearTimeout(timeoutId)

    if (!response.ok || !response.body) {
      const kind = classifyHttpFailure(response.status)
      scheduleReconnect(kind)
      return
    }

    const wasReconnect = _everConnected || reconnectAttempts > 0
    _everConnected = true
    reconnectAttempts = 0
    connected.value = true
    connectionState.value = 'connected'
    if (wasReconnect) {
      // Successful reconnect (FAR-250): backfill unread count + dropdown
      // via the debounced REST refetch — /api/v1/events has no replay buffer.
      scheduleReconnectBackfill()
    }

    reader = response.body.getReader()

    for await (const { event, data } of parseSSEStream(reader)) {
      if (event === 'resource_changed') {
        handleResourceChanged(data)
      }
    }
  } catch (e: unknown) {
    handleConnectError(timeoutId, e)
    return
  } finally {
    reader?.cancel().catch(() => {})
    connected.value = false
    if (connectionState.value === 'connected') {
      connectionState.value = 'reconnecting'
    }
  }

  // Server closed the stream (or parser ended): zombie path — retry forever.
  // (The 4xx/5xx classified paths `return` inside the try above, so an
  // auth_failed state never reaches this line.)
  if (!_disconnecting) scheduleReconnect('retry')
}

function cleanup(): void {
  clearReconnectTimer()
  if (abortController) {
    abortController.abort()
    abortController = null
  }
  connected.value = false
}

function disconnect(): void {
  _disconnecting = true
  cleanup()
  connectionState.value = 'idle'
  clearAllHandlers()
  if (backfillTimer) {
    clearTimeout(backfillTimer)
    backfillTimer = null
  }
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
  get state(): EventStreamState { return connectionState.value },
  /** True when the stream stopped on a 4xx and needs a banner restart. */
  get reconnectRequired(): boolean { return connectionState.value === 'auth_failed' },
  subscribe(resourceType: string, handler: EventHandler): () => void {
    if (!handlers.has(resourceType)) handlers.set(resourceType, new Set())
    handlers.get(resourceType)!.add(handler)
    // connect() owns the "already connected / auth-stopped" guards so a second
    // subscriber cannot open a competing stream.
    connect()
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
  /** Manual restart (banner button): clears the auth-failed stop with a fresh token. */
  reconnect(): void {
    cleanup()
    reconnectAttempts = 0
    _disconnecting = false
    connectionState.value = 'connecting'
    doConnect()
  },
  /**
   * Register a callback fired (debounced as one batch) after each successful
   * RECONNECT — use it to REST-backfill state the missed stream window
   * covered. Returns an unsubscribe function.
   */
  onReconnect(cb: () => void): () => void {
    backfillSubscribers.add(cb)
    return () => { backfillSubscribers.delete(cb) }
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
  return { connected, connectionState }
}

export { dispatchToStore }

/**
 * Reset all module-level stream state. Used by the HMR dispose hook so a hot
 * reload does not leak handlers, a pending backfill timer, or a stale
 * connection state. Exported so the reset can be exercised directly in tests
 * (the HMR branch itself never executes under vitest).
 */
export function resetEventStreamState(): void {
  cleanup()
  clearAllHandlers()
  backfillSubscribers.clear()
  if (backfillTimer) {
    clearTimeout(backfillTimer)
    backfillTimer = null
  }
  connectionState.value = 'idle'
  _everConnected = false
}

if (import.meta.hot) {
  import.meta.hot.dispose(resetEventStreamState)
}
