import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ANALYTICS_EVENTS } from '../../composables/productAnalyticsEvents'

const EVENTS_API = '/api/v1/metrics/events'

let useProductAnalytics: typeof import('../../composables/useProductAnalytics').useProductAnalytics
let initProductAnalytics: typeof import('../../composables/useProductAnalytics').initProductAnalytics

beforeEach(async () => {
  vi.resetModules()
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }))
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.useFakeTimers()
  const mod = await import('../../composables/useProductAnalytics')
  useProductAnalytics = mod.useProductAnalytics
  initProductAnalytics = mod.initProductAnalytics
})

afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})


describe('useProductAnalytics', () => {
  describe('consent gate', () => {
    it('does not buffer events when consent function returns false', () => {
      initProductAnalytics(() => false)
      const { track } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)

      expect(globalThis.fetch).not.toHaveBeenCalled()
    })

    it('does not buffer events when consent function is not initialised', () => {
      const { track } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)

      expect(globalThis.fetch).not.toHaveBeenCalled()
    })

    it('does not buffer events when consent function throws', () => {
      initProductAnalytics(() => {
        throw new Error('store not ready')
      })
      const { track } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)

      expect(globalThis.fetch).not.toHaveBeenCalled()
    })

    it('buffers events when consent function returns true', () => {
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      flush()

      expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    })
  })

  describe('event buffering', () => {
    it('buffers a single event with correct shape', () => {
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED, { pipeline_id: 'p-1' })
      flush()

      const [, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body.events).toHaveLength(1)
      expect(body.events[0]).toMatchObject({
        event_id: '1',
        event_type: 'pipeline_created',
        payload: { pipeline_id: 'p-1' },
      })
      expect(body.events[0].recorded_at).toBeTruthy()
    })

    it('buffers events without payload when none provided', () => {
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_RUN_STARTED)
      flush()

      const [, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body.events[0].payload).toBeUndefined()
    })

    it('accumulates multiple events in the buffer', () => {
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      track(ANALYTICS_EVENTS.SCHEMA_CREATED)
      track(ANALYTICS_EVENTS.CONNECTOR_ADDED)
      flush()

      const [, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body.events).toHaveLength(3)
    })
  })

  describe('flush', () => {
    it('does not call fetch when buffer is empty', () => {
      initProductAnalytics(() => true)
      const { flush } = useProductAnalytics()

      flush()

      expect(globalThis.fetch).not.toHaveBeenCalled()
    })

    it('sends batch to the correct API endpoint', () => {
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      flush()

      expect(globalThis.fetch).toHaveBeenCalledWith(
        EVENTS_API,
        expect.objectContaining({
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
        }),
      )
    })

    it('clears the buffer after flushing', () => {
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      flush()
      flush()

      expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    })

    it('exposes a reactive bufferLength that grows on track and resets on flush', () => {
      initProductAnalytics(() => true)
      const { track, flush, bufferLength } = useProductAnalytics()

      expect(bufferLength.value).toBe(0)

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      track(ANALYTICS_EVENTS.SCHEMA_CREATED)
      expect(bufferLength.value).toBe(2)

      flush()
      expect(bufferLength.value).toBe(0)
    })
  })

  describe('buffer full threshold', () => {
    it('auto-flushes when 50 events accumulate', () => {
      initProductAnalytics(() => true)
      const { track } = useProductAnalytics()

      for (let i = 0; i < 49; i++) {
        track(ANALYTICS_EVENTS.API_ERROR)
      }
      expect(globalThis.fetch).not.toHaveBeenCalled()

      track(ANALYTICS_EVENTS.API_ERROR)
      expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    })
  })

  describe('interval flush', () => {
    it('flushes on the 30-second interval', () => {
      initProductAnalytics(() => true)
      const { track } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      expect(globalThis.fetch).not.toHaveBeenCalled()

      vi.advanceTimersByTime(30_000)
      expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    })

    it('continues flushing on subsequent intervals', () => {
      initProductAnalytics(() => true)
      const { track } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      vi.advanceTimersByTime(30_000)
      expect(globalThis.fetch).toHaveBeenCalledTimes(1)

      track(ANALYTICS_EVENTS.SCHEMA_CREATED)
      vi.advanceTimersByTime(30_000)
      expect(globalThis.fetch).toHaveBeenCalledTimes(2)
    })
  })

  describe('event ID generation', () => {
    it('uses counter-based IDs starting from 1', () => {
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      track(ANALYTICS_EVENTS.SCHEMA_CREATED)
      track(ANALYTICS_EVENTS.CONNECTOR_ADDED)
      flush()

      const [, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body.events[0].event_id).toBe('1')
      expect(body.events[1].event_id).toBe('2')
      expect(body.events[2].event_id).toBe('3')
    })

    it('increments counter across flushes', () => {
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      flush()

      track(ANALYTICS_EVENTS.SCHEMA_CREATED)
      flush()

      const [, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[1]
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body.events[0].event_id).toBe('2')
    })
  })

  describe('error handling', () => {
    it('logs to console.warn when fetch fails', async () => {
      ;(globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
        new Error('network error'),
      )
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)
      flush()
      vi.advanceTimersByTime(0)
      await Promise.resolve()

      expect(console.warn).toHaveBeenCalledWith(
        '[product-analytics] failed to flush events',
        expect.any(Error),
      )
    })

    it('does not throw when fetch fails', () => {
      ;(globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
        new Error('network error'),
      )
      initProductAnalytics(() => true)
      const { track, flush } = useProductAnalytics()

      track(ANALYTICS_EVENTS.PIPELINE_CREATED)

      expect(() => flush()).not.toThrow()
    })
  })

  describe('event types constant', () => {
    it('contains all expected event types', () => {
      expect(ANALYTICS_EVENTS).toEqual({
        PIPELINE_RUN_STARTED: 'pipeline_run_started',
        PIPELINE_CREATED: 'pipeline_created',
        PIPELINE_GRAPH_SAVED: 'pipeline_graph_saved',
        HITL_GATE_CLAIMED: 'hitl_gate_claimed',
        HITL_GATE_APPROVED: 'hitl_gate_approved',
        HITL_GATE_REJECTED: 'hitl_gate_rejected',
        GUARDRAIL_OVERRIDDEN: 'guardrail_overridden',
        SCHEMA_CREATED: 'schema_created',
        CONNECTOR_ADDED: 'connector_added',
        MODEL_BACKEND_ADDED: 'model_backend_added',
        TRIGGER_CREATED: 'trigger_created',
        VARIANT_BATCH_FIRED: 'variant_batch_fired',
        EVAL_CREATED: 'eval_created',
        API_ERROR: 'api_error',
      })
    })
  })
})
