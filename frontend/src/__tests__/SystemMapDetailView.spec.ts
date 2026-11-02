import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'

const mockGet = vi.hoisted(() => vi.fn())
const mockGetAccessToken = vi.hoisted(() => vi.fn())
const mockIsOperator = vi.hoisted(() => ({ value: true }))
const mockIsSystemAdmin = vi.hoisted(() => ({ value: false }))
const mockPlanStore = vi.hoisted(() => ({
  loaded: true,
  currentTier: 'community',
  devMode: true,
  userPermissions: ['system_maps_access', 'system.config.manage'],
  fetchPlan: vi.fn(),
  fetchPermissions: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: mockGetAccessToken,
}))
vi.mock('../stores/planStore', () => ({ usePlanStore: () => mockPlanStore }))
vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({ isOperator: mockIsOperator, isSystemAdmin: mockIsSystemAdmin }),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'map-1' } }),
  useRouter: () => ({ push: vi.fn() }),
  RouterLink: { template: '<a><slot /></a>' },
}))

import SystemMapDetailView from '../views/SystemMapDetailView.vue'
import enUS from '../locales/en-US.js'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

function mountView() {
  return mount(SystemMapDetailView, {
    global: {
      plugins: [i18n],
      stubs: {
        BackLink: true,
        HeaderBar: true,
        GraphEditor: true,
        RevisionSidebar: true,
        DiscoveryPanel: true,
      },
    },
  })
}

describe('SystemMapDetailView load failures', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
  })

  it('offers an inline retry that re-runs the failed load', async () => {
    let mapCalls = 0
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      }
      mapCalls += 1
      if (mapCalls === 1) return Promise.resolve({ error: { detail: 'map unavailable' } })
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })

    const wrapper = mountView()
    await flushPromises()

    const retry = wrapper.find('[data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)

    await retry.trigger('click')
    await flushPromises()

    expect(mapCalls).toBe(2)
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(true)
  })
})
