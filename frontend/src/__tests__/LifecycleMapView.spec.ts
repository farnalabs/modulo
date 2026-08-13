import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createI18n } from 'vue-i18n'

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({ params: { id: 'map-1' }, query: {}, meta: {}, name: 'lifecycle-map-detail' })),
  useRouter: vi.fn(() => ({ push: routerPushMock })),
}))

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  redirectToLogin: vi.fn(),
}))

import LifecycleMapView from '../views/lifecycle-map/LifecycleMapView.vue'

const routerPushMock = vi.fn()

function okJson(data: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => data,
  } as unknown as Response
}

const mapDetail = {
  id: 'map-1',
  name: 'Launch Flow',
  description: null,
  owner: null,
  owner_team_id: null,
  stages: [{ id: 'stage-1', name: 'Build', description: null, type: 'modulo', owner_badge: null, graduated: false, pipeline_id: null, external_url: null }],
  transitions: [],
  versions: [{ version: 1, created_at: '2026-01-01T00:00:00Z', created_by: null }],
  current_version: 1,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      views: {
        LifecycleMapView: {
          edit: 'Edit',
          version_label: 'Version:',
        },
      },
    },
  },
})

beforeEach(() => {
  setActivePinia(createPinia())
  routerPushMock.mockClear()
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
    return Promise.resolve(okJson(mapDetail))
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('LifecycleMapView', () => {
  it('renders an Edit button that routes to the editor', async () => {
    const wrapper = mount(LifecycleMapView, {
      global: {
        plugins: [i18n],
        stubs: {
          RouterLink: { template: '<a><slot /></a>' },
          PageHeader: true,
          ErrorAlert: true,
          LifecycleMapRenderer: true,
          JourneyCard: true,
          ProvenanceBadge: true,
        },
      },
    })
    await flushPromises()

    const editBtn = wrapper.find('[data-testid="lifecycle-map-view-edit"]')
    expect(editBtn.exists()).toBe(true)

    await editBtn.trigger('click')
    expect(routerPushMock).toHaveBeenCalledWith({ name: 'lifecycle-map-editor', params: { id: 'map-1' } })
  })
})
