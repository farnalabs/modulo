import { computed, reactive, readonly } from 'vue'
import type { Ref } from 'vue'
import type { AnalyticsEventType } from './productAnalyticsEvents'
import { randomId } from '../lib/randomId'

/**
 * Product analytics event capture composable (FAR-358).
 *
 * Curated frontend events are buffered in memory and flushed to
 * `POST /api/v1/metrics/events` on interval (30s) or buffer full (50 events).
 * The backend stages them in `metrics_staging`; the daily metrics dump
 * aggregates them into the vendor payload and purges consumed rows (FAR-1717).
 *
 * Consent gate: the composable checks a configurable consent function before
 * capturing. When `level=off`, `track()` silently returns — no buffer, no API
 * call. `initProductAnalytics` is called from `App.vue` with
 * `productAnalyticsStore.isOptedIn` as the consent function (FAR-1718).
 *
 * No `page_view` event is captured: no per-route data is collected, client or
 * server side. The captured set is exactly the curated list in
 * `productAnalyticsEvents.ts`.
 *
 * No PII is captured: no names, emails, user IDs, IPs, URLs, query strings.
 * The backend applies the event-type payload allowlist again at the ingest
 * boundary; client payloads therefore remain curated even if a caller passes
 * extra fields.
 * `event_id` is a per-session prefix plus a monotonic counter, serialised as a
 * string. A bare counter is not unique: the backend dedups on
 * (organisation_id, event_id), so a counter restarting at 1 on every page load
 * (or in a second tab) made every later session's events collide with the
 * first and be silently dropped. The prefix keeps IDs unique across sessions
 * without a random UUID per event.
 * API failures are logged to console.warn, never thrown.
 */

const EVENTS_API = '/api/v1/metrics/events'
const FLUSH_INTERVAL_MS = 30_000
const BUFFER_FULL_THRESHOLD = 50

interface AnalyticsEvent {
  event_id: string
  event_type: AnalyticsEventType
  recorded_at: string
  payload?: Record<string, unknown>
}

const _buffer = reactive<AnalyticsEvent[]>([])
let _flushTimer: ReturnType<typeof setInterval> | null = null
let _eventIdCounter = 0
const _sessionPrefix = randomId()
let _consentFn: (() => boolean) | null = null
let _visibilityHandler: (() => void) | null = null

function _isConsented(): boolean {
  if (!_consentFn) return false
  try {
    return _consentFn()
  } catch {
    return false
  }
}

function _flush(keepalive = false): void {
  if (_buffer.length === 0) return
  const batch = _buffer.splice(0)
  fetch(EVENTS_API, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ events: batch }),
    credentials: 'include',
    keepalive,
  }).catch((err: unknown) => {
    console.warn('[product-analytics] failed to flush events', err)
  })
}

/**
 * Initialise product analytics capture. Call once from App.vue (or equivalent
 * root component) after the consent store is available.
 *
 * @param consentFn - Returns `true` when the current org has opted in
 *                    (`level=all`). Returns `false` for `level=off` or
 *                    when consent state is unknown.
 */
export function initProductAnalytics(consentFn: () => boolean): void {
  _consentFn = consentFn
  if (_flushTimer) return
  _flushTimer = setInterval(_flush, FLUSH_INTERVAL_MS)

  _visibilityHandler = () => {
    if (document.visibilityState === 'hidden') {
      _flush(true)
    }
  }
  document.addEventListener('visibilitychange', _visibilityHandler)
}

/**
 * Composable returning a `track` function for product analytics events.
 *
 * Usage:
 * ```ts
 * const { track } = useProductAnalytics()
 * track(ANALYTICS_EVENTS.PIPELINE_CREATED, { pipeline_id: '...' })
 * ```
 */
export function useProductAnalytics(): {
  track: (eventType: AnalyticsEventType, payload?: Record<string, unknown>) => void
  flush: () => void
  bufferLength: Readonly<Ref<number>>
} {
  function track(eventType: AnalyticsEventType, payload?: Record<string, unknown>): void {
    if (!_isConsented()) return

    _eventIdCounter++
    const event: AnalyticsEvent = {
      event_id: `${_sessionPrefix}-${_eventIdCounter}`,
      event_type: eventType,
      recorded_at: new Date().toISOString(),
    }
    if (payload !== undefined) {
      event.payload = payload
    }

    _buffer.push(event)

    if (_buffer.length >= BUFFER_FULL_THRESHOLD) {
      _flush()
    }
  }

  return {
    track,
    flush: _flush,
    bufferLength: readonly(computed(() => _buffer.length)),
  }
}
