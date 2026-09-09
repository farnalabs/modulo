import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// Restore the REAL vue-router (the shared vitest setup mocks it globally) so
// the actual beforeEach guard in ../router/index.ts runs — same override
// pattern as app-bootstrap.spec.ts / demo-handoff.spec.ts.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

// FAR-656: cover the manifest-declared route-flag guard (router/index.ts lines
// ~666-677). The guard reads the plan store, so we stub it with a deterministic
// featureEnabled/loaded so the flag-gated redirect branch (and the plan-fetch
// branch when the plan is not yet loaded) is exercised without a live backend.
const { planStoreStub, featureEnabled } = vi.hoisted(() => {
  const featureEnabled = vi.fn((_name: string) => false)
  const planStoreStub = {
    loaded: true,
    features: {} as Record<string, unknown>,
    devMode: false,
    isAtMinimumTier: () => true,
    featureEnabled,
    fetchPlan: vi.fn(async () => {}),
  }
  return { planStoreStub, featureEnabled }
})

vi.mock('../stores/planStore', () => ({
  usePlanStore: vi.fn(() => planStoreStub),
}))

import router from '../router'
import { clearAccessToken, setAccessToken } from '../lib/api/auth'

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  clearAccessToken()
  featureEnabled.mockReturnValue(false)
  planStoreStub.loaded = true
})

describe('FAR-656: manifest route feature_flag guard', () => {
  it('redirects a feature-flagged route to the dashboard when its flag is disabled', async () => {
    setAccessToken('real-jwt-token')
    await router.push({ name: 'admin-notification-delivery' })
    expect(router.currentRoute.value.name).toBe('dashboard')
  }, 20_000)

  it('triggers a plan fetch (and still redirects) when the flag is disabled and the plan is not yet loaded', async () => {
    setAccessToken('real-jwt-token')
    planStoreStub.loaded = false
    await router.push({ name: 'admin-notification-delivery' })
    expect(router.currentRoute.value.name).toBe('dashboard')
    expect(planStoreStub.fetchPlan).toHaveBeenCalled()
  }, 20_000)

  it('allows the route through when its flag is enabled', async () => {
    setAccessToken('real-jwt-token')
    featureEnabled.mockReturnValue(true)
    await router.push({ name: 'admin-notification-delivery' })
    expect(router.currentRoute.value.name).toBe('admin-notification-delivery')
  }, 20_000)
})
