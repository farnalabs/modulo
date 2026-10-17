import { ref, onMounted, onUnmounted } from 'vue'
import type { EventBusEvent } from '@/types/events'
import { getHandlers } from '@/stores/syncRegistry'
import { getAuthHeaders, attemptTokenRefresh, onAuthChange } from '@/lib/api/client'
import { parseSSEStream } from '@/lib/sse'

export type EventHandler = (event: EventBusEvent) => void

/** Stream lifecycle state (FAR-250 classified reconnect). */
export type EventStreamState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'auth_failed'

/**
 * Feedback for a MANUAL reconnect (banner button), so the click is visibly
 * acknowledged: 'attempting' while the restart is in flight, 'failed' when it
 * lands back on a terminal auth stop (FAR-1842). Cleared on success / a
 * retry-classified failure (the stream is self-healing again) / disconnect.
 */
export type ReconnectFeedback = 'idle' | 'attempting' | 'failed'

const SSE_URL = '/api/v1/events'

const connected = ref(false)
const connectionState = ref<EventStreamState>('idle')
const reconnectFeedback = ref<ReconnectFeedback>('idle')
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
 * Classify a failed HTTP response (FAR-250, corrected by FAR-1842):
 * - 401/403 -> terminal auth failure: STOP retrying and surface the
 *   reconnect banner; a fresh sign-in is required.
 * - 429 (rate-limit / connection cap) and every other 4xx -> TRANSIENT:
 *   retry with capped backoff like 5xx. Only a genuine auth rejection
 *   demands a fresh sign-in.
 * - 5xx and everything else -> retry forever with capped backoff + jitter.
 *
 * A likely-permanent non-auth 4xx keeps the 'retry' classification but is no
 * longer silent — the call site warns through warnPersistentClientError
 * (FAR-1861).
 */
function classifyHttpFailure(status: number | undefined): 'auth' | 'retry' {
  if (status === 401 || status === 403) {
    return 'auth'
  }
  return 'retry'
}

/**
 * Consecutive-retry signal for a NON-auth 4xx (FAR-1861). Classification is
 * unchanged — only 401/403 are terminal, 429 stays transient, everything
 * else retries forever — but a 400/404 that loops every backoff tick must not
 * be SILENT: warn on the first occurrence of a status and then every 10th
 * retry, so a permanently-rejected endpoint is visible in the console without
 * a line per tick. Reset on every successful connect (and by the HMR reset).
 * 429 is deliberately excluded — it is expected-transient rate limiting and
 * must never read as a permanent client error.
 */
let clientErrorStatus = 0
let clientErrorRetries = 0

function warnPersistentClientError(status: number): void {
  if (clientErrorStatus === status) {
    clientErrorRetries++
  } else {
    clientErrorStatus = status
    clientErrorRetries = 1
  }
  if (clientErrorRetries === 1 || clientErrorRetries % 10 === 0) {
    console.warn(
      `[EventBus] Reconnect received HTTP ${status}: a non-auth 4xx is usually permanent, ` +
        `but the stream still retries by design (attempt ${clientErrorRetries}). ` +
        `If this persists, fix the endpoint or token — 401/403 stop the stream instead.`,
    )
  }
}

/** True for a 4xx that signals a likely-permanent client error (not rate-limiting). */
function isPersistentClientError(status: number | undefined): boolean {
  return status !== undefined && status >= 400 && status < 500 && status !== 429
}

/**
 * Classify a failed connect and schedule the retry. A likely-permanent
 * non-auth 4xx (400/404/… — not 429) retries forever, so warn before it
 * loops silently (FAR-1861). Extracted from doConnect so the extra branch
 * does not push that function's cognitive complexity over Sonar S3776.
 */
function handleFailedConnect(status: number): void {
  const kind = classifyHttpFailure(status)
  if (kind === 'retry' && isPersistentClientError(status)) {
    warnPersistentClientError(status)
  }
  scheduleReconnect(kind)
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
    // A manual restart that lands back on a terminal stop must SAY so —
    // the banner re-shows with the failed feedback (FAR-1842).
    if (reconnectFeedback.value === 'attempting') {
      reconnectFeedback.value = 'failed'
    }
    console.warn('[EventBus] Stopping stream: server rejected it (401/403). Restart with a fresh token.')
    return
  }
  // A retry-classified failure is self-healing: clear any in-flight manual
  // feedback so the banner does not linger over an automatic retry loop.
  if (reconnectFeedback.value === 'attempting') {
    reconnectFeedback.value = 'idle'
  }
  connectionState.value = 'reconnecting'
  reconnectAttempts++
  // Capped exponential backoff with half-jitter; NEVER gives up (the old
  // 10-attempt cap was the bug — long outages must self-heal).
  const exp = Math.min(BACKOFF_BASE_MS * 2 ** Math.min(reconnectAttempts - 1, 10), BACKOFF_MAX_MS)
  const delay = exp / 2 + secureRandomFraction() * (exp / 2)
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    void doConnect()
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
  void doConnect()
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
  // connect() blocks while auth_failed and restartStream()/reconnect() clear
  // it, so doConnect is only ever reached from a non-auth-stopped state.
  connectionState.value = 'connecting'
  let timeoutId = setTimeout(() => abortController?.abort(), FETCH_TIMEOUT_MS)
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

  try {
    let response = await fetch(SSE_URL, {
      headers: { ...getAuthHeaders() },
      signal: abortController.signal,
    })
    // The stream does NOT ride the API client's withAuth 401-refresh loop, so
    // serialise through the SAME attemptTokenRefresh() here (FAR-1842): a
    // normal access-token expiry re-issues the fetch once with the fresh
    // token instead of raising the terminal banner. 403 is a permission
    // rejection, not an expiry — no refresh, straight to classification.
    if (response.status === 401) {
      // The 30s connect timeout guards the SSE fetch itself; it must NOT fire
      // while a token refresh is in flight (FAR-1861). The refresh runs on
      // its own request (this abort signal is not passed to it), so a timeout
      // landing mid-refresh could not stop the refresh — it could only flip
      // `signal.aborted` and discard a refresh that was about to succeed,
      // pushing a recoverable expiry onto the terminal banner. Clear the
      // timeout for the refresh, re-arm it for the retried fetch.
      clearTimeout(timeoutId)
      const refreshed = await attemptTokenRefresh()
      if (refreshed && !abortController.signal.aborted) {
        timeoutId = setTimeout(() => abortController?.abort(), FETCH_TIMEOUT_MS)
        response = await fetch(SSE_URL, {
          headers: { ...getAuthHeaders() },
          signal: abortController.signal,
        })
      }
    }
    clearTimeout(timeoutId)

    if (!response.ok || !response.body) {
      // A likely-permanent 4xx (400/404/... — not 429) retries forever, so
      // say so loudly instead of looping silently (FAR-1861).
      handleFailedConnect(response.status)
      return
    }

    const wasReconnect = _everConnected || reconnectAttempts > 0
    _everConnected = true
    reconnectAttempts = 0
    // A healthy connect proves the endpoint answers again — restart the
    // non-auth-4xx streak so the next outage warns from its first attempt.
    clientErrorStatus = 0
    clientErrorRetries = 0
    connected.value = true
    connectionState.value = 'connected'
    // Manual restart recovered: clear the feedback so the banner disappears
    // with the successful connection (it is only visible again on failure).
    if (reconnectFeedback.value === 'attempting') {
      reconnectFeedback.value = 'idle'
    }
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
  reconnectFeedback.value = 'idle'
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

/**
 * Tear down and immediately re-run the connect attempt, bypassing the
 * auth_failed stop in connect(). Shared by the manual reconnect button and
 * the auth-change re-arm below.
 */
function restartStream(): void {
  cleanup()
  reconnectAttempts = 0
  _disconnecting = false
  connectionState.value = 'connecting'
  void doConnect()
}

// Auth-change re-arm (FAR-1842): auth_failed is only valid for as long as the
// session that hit it. A fresh non-null token (re-login, SSO callback, or a
// successful refresh) supersedes the terminal stop — re-arm the stream so the
// banner cannot outlive the condition that raised it, on ANY route. The
// unsubscribe is retained so the HMR dispose hook below drops the listener
// with the rest of the module state — otherwise a hot reload accumulates
// listeners bound to a previous module closure.
const disposeAuthChangeHook = onAuthChange((token: string | null) => {
  if (token && connectionState.value === 'auth_failed') {
    reconnectFeedback.value = 'idle'
    restartStream()
  }
})

export const eventBus = {
  get connected() { return connected.value },
  get state(): EventStreamState { return connectionState.value },
  /** True when the stream stopped on 401/403 and needs a banner restart. */
  get reconnectRequired(): boolean { return connectionState.value === 'auth_failed' },
  /** Manual-reconnect feedback ('idle' | 'attempting' | 'failed'), FAR-1842. */
  get reconnectFeedback(): ReconnectFeedback { return reconnectFeedback.value },
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
    // Feedback first so the click is synchronously acknowledged before the
    // async restart settles (FAR-1842).
    reconnectFeedback.value = 'attempting'
    restartStream()
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
  disposeAuthChangeHook()
  cleanup()
  clearAllHandlers()
  backfillSubscribers.clear()
  clientErrorStatus = 0
  clientErrorRetries = 0
  if (backfillTimer) {
    clearTimeout(backfillTimer)
    backfillTimer = null
  }
  connectionState.value = 'idle'
  reconnectFeedback.value = 'idle'
  _everConnected = false
}

if (import.meta.hot) {
  import.meta.hot.dispose(resetEventStreamState)
}
