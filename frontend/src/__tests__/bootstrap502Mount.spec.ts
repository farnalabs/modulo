/**
 * FAR-1888 — does a 502 on a bootstrap call block the SPA mount?
 *
 * The hypothesis under test: `main.ts` mounts only after `await router.isReady()`,
 * and the router guard awaits `planStore.fetchPlan()` (router/index.ts:759), so a
 * bootstrap 502 sits on the first-mount path and could strand `#app` empty.
 *
 * These tests exercise the REAL guard callback (retrieved from the callback
 * `router/index.ts` registered on `router.beforeEach`) and the REAL plan store,
 * stubbing only the API client so every bootstrap read returns the shape a 502
 * from the Vite dev proxy produces. They pin the observed invariant:
 *
 *   1. a 502 settles `/api/v1/plan` terminally as `flagsSource === 'unavailable'`;
 *   2. the guard FAILS OPEN for an authenticated tier-gated route (FAR-1809:
 *      the tier check is gated on `planStore.loaded &&`, and a failed fetch never
 *      sets `loaded`), returning `true` rather than a redirect;
 *   3. the navigation therefore SETTLES — so `router.isReady()` resolves and
 *      `main.ts` mounts;
 *   4. the unauthenticated first-goto path (a fresh Playwright context with no
 *      token, which is what the failing e2e route sweep hits) never issues the
 *      `/plan` call at all — the guard short-circuits to `login`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter } from 'vue-router'
import { enforceRoleTierVisibility } from '../router'
import { usePlanStore } from '../stores/planStore'
import { clearAllRegistrations } from '../stores/syncRegistry'
import { getAccessToken } from '../lib/api/client'

// Spread the real module and override only the two bindings under test, so every
// other consumer of the client (auth helpers re-exported from `./auth`, the demo
// entry resolver, LoginView's imports) still resolves.
vi.mock('../lib/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api/client')>()
  return {
    ...actual,
    api: { ...actual.api, GET: vi.fn() },
    getAccessToken: vi.fn(),
  }
})

type GuardRoute = Parameters<typeof enforceRoleTierVisibility>[0]
type Guard = (to: GuardRoute) => Promise<unknown>
type RouterStub = { beforeEach: { mock: { calls: Array<[Guard]> } } }

function encodeSegment(payload: Record<string, unknown>): string {
  return btoa(JSON.stringify(payload)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** Build a minimal opaque-JWT string carrying the given claims. */
function jwt(payload: Record<string, unknown>): string {
  return `${encodeSegment({ alg: 'HS256', typ: 'JWT' })}.${encodeSegment(payload)}.sig`
}

function route(meta: Record<string, unknown>, name = 'far-1888-unknown-route'): GuardRoute {
  return { name, meta } as unknown as GuardRoute
}

/** Stub every API read with the openapi-fetch result shape a 502 produces. */
async function mockBootstrap502(): Promise<void> {
  const { api } = await import('../lib/api/client')
  const GET = api.GET as unknown as ReturnType<typeof vi.fn>
  GET.mockImplementation(() =>
    Promise.resolve({
      data: undefined,
      error: { title: 'Bad Gateway', status: 502, detail: 'Bad Gateway' },
      response: { status: 502 },
    }),
  )
}

/** The REAL guard callback `router/index.ts` registered on the shared router stub. */
function registeredGuard(): Guard {
  const stub = (createRouter as unknown as { mock: { results: Array<{ value: RouterStub }> } })
    .mock.results[0]?.value
  if (!stub) throw new Error('router/index.ts did not construct a router')
  const call = stub.beforeEach.mock.calls[0]
  if (!call) throw new Error('router/index.ts did not register a beforeEach guard')
  return call[0]
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  clearAllRegistrations()
  vi.mocked(getAccessToken).mockReturnValue(null)
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('FAR-1888 — a 502 on a bootstrap call cannot block the SPA mount', () => {
  it('settles `/api/v1/plan` terminally as `unavailable` — resolves, never rejects, never burns the 15s bound', async () => {
    await mockBootstrap502()
    const store = usePlanStore()

    const started = Date.now()
    await expect(store.fetchPlan()).resolves.toBeUndefined()
    const elapsed = Date.now() - started

    expect(store.loaded).toBe(false)
    expect(store.flagsSource).toBe('unavailable')
    expect(elapsed).toBeLessThan(15000)
    expect(console.warn).toHaveBeenCalledWith(
      expect.stringContaining('[plan] Feature flags request settled without a payload'),
      expect.anything(),
    )
  })

  it('fails OPEN on a 502 for an authenticated tier-gated route — returns `true`, not a redirect', async () => {
    await mockBootstrap502()
    const token = jwt({ org_id: 'acme', org_role: 'admin' })
    vi.mocked(getAccessToken).mockReturnValue(token)

    const result = await enforceRoleTierVisibility(route({ requiredTier: 'team' }), token)

    expect(result).toBe(true)
    const store = usePlanStore()
    expect(store.loaded).toBe(false)
    expect(store.flagsSource).toBe('unavailable')
  })

  it('the real registered guard callback SETTLES on a 502 — so router.isReady() resolves and main.ts mounts', async () => {
    await mockBootstrap502()
    const { api } = await import('../lib/api/client')
    const GET = api.GET as unknown as ReturnType<typeof vi.fn>
    GET.mockClear()
    vi.mocked(getAccessToken).mockReturnValue(jwt({ org_id: 'acme', org_role: 'admin' }))
    const guard = registeredGuard()

    const started = Date.now()
    await expect(guard(route({ requiredTier: 'team' }))).resolves.toBeUndefined()
    expect(Date.now() - started).toBeLessThan(15000)

    // Non-vacuity: the retrieved callback must be the REAL guard, so it must have
    // driven the store's bootstrap fetch. A no-op stub would resolve to
    // `undefined` too, so this assertion is what makes the test discriminating.
    expect(GET.mock.calls.map((call: unknown[]) => call[0])).toContain('/api/v1/plan')
  })

  it('the unauthenticated first-goto path never issues the `/plan` bootstrap call — it redirects to login', async () => {
    await mockBootstrap502()
    const { api } = await import('../lib/api/client')
    const GET = api.GET as unknown as ReturnType<typeof vi.fn>
    GET.mockClear()
    vi.mocked(getAccessToken).mockReturnValue(null)

    const guard = registeredGuard()
    await expect(guard(route({}, 'pipelines'))).resolves.toEqual({ name: 'login' })

    const paths = GET.mock.calls.map((call: unknown[]) => call[0])
    expect(paths).not.toContain('/api/v1/plan')
  })
})
