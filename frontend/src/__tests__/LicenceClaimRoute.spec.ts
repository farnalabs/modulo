import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// The real router + guard: this spec proves the PUBLIC claim route is reachable
// by an anonymous browser, not merely that a route record exists.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})
const { usePlanStore } = vi.hoisted(() => ({ usePlanStore: vi.fn() }))
vi.mock('../lib/jwt', () => ({ decodeJwtPayload: vi.fn(() => null) }))
vi.mock('../stores/planStore', () => ({ usePlanStore }))

import router from '../router'
import { clearAccessToken } from '../lib/api/auth'

const planStore = {
  loaded: true,
  features: {} as Record<string, unknown>,
  devMode: false,
  userPermissions: [] as string[],
  isAtMinimumTier: vi.fn(() => true),
  featureEnabled: vi.fn(() => false),
  fetchPlan: vi.fn(async () => {}),
  fetchPermissions: vi.fn(async () => {}),
}

beforeEach(() => {
  vi.clearAllMocks()
  clearAccessToken()
  usePlanStore.mockReturnValue(planStore)
})

describe('public licence-claim route', () => {
  it('is reachable without a session and is not bounced to /login', async () => {
    await router.push('/licence/claim')

    expect(router.currentRoute.value.name).toBe('licence-claim')
    expect(router.currentRoute.value.meta.public).toBe(true)
  })

  it('is rendered through <router-view> by App.vue while unauthenticated', () => {
    // Regression guard: meta.public satisfies the navigation guard, but App.vue
    // only renders <router-view> for the routes in ROUTE_VIEW_PUBLIC_ROUTES —
    // an anonymous visitor to a public route missing from that set silently
    // gets LoginView instead of the page.
    const appSource = readFileSync(resolve(__dirname, '../App.vue'), 'utf-8')
    const setMatch = appSource.match(/const ROUTE_VIEW_PUBLIC_ROUTES = new Set\(\[([^\]]*)\]\)/)
    expect(setMatch).not.toBeNull()
    expect(setMatch![1]).toContain("'licence-claim'")
  })
})
