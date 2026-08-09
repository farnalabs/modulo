import { describe, it, expect, vi } from 'vitest'

// The shared vitest setup (src/__tests__/setup.ts) mocks 'vue-router' globally
// so component tests can call useRouter()/useRoute() without a real router.
// The app-bootstrap smoke test's job is the opposite: import the REAL router
// module (frontend/src/router/index.ts) and verify every import resolves and
// every route component factory can be loaded. A file-level vi.mock overrides
// the setup mock for this file only, restoring the real vue-router.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

import router from '../router'

describe('app bootstrap', () => {
  it('router module imports resolve and the route table is populated', () => {
    expect(router).toBeDefined()
    const routes = router.getRoutes()
    expect(routes.length).toBeGreaterThan(0)
  })

  it('every route component factory resolves to a module', async () => {
    const routes = router.getRoutes()
    for (const route of routes) {
      for (const component of Object.values(route.components ?? {})) {
        if (typeof component === 'function') {
          const module = await (component as () => Promise<unknown>)()
          expect(module, `lazy route ${route.path} component failed to resolve`).toBeTruthy()
        }
      }
      // Statically imported components (LoginView, OAuthConsentView) already
      // resolved when the router module was imported above; redirect-only
      // routes carry no components and are covered by the redirect test below.
    }
  }, 60_000)

  it('routes without a component define a redirect', () => {
    const routes = router.getRoutes()
    const withoutComponent = routes.filter((route) => {
      const components = route.components
      return !components || Object.keys(components).length === 0
    })
    expect(withoutComponent.length).toBeGreaterThan(0)
    for (const route of withoutComponent) {
      expect(route.redirect, `route ${route.path} has no component or redirect`).toBeDefined()
    }
  })
})
