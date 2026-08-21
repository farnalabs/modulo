import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createI18n } from 'vue-i18n'

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({ params: {}, query: {}, meta: {}, name: 'lifecycle-maps' })),
  useRouter: vi.fn(() => ({ push: routerPushMock })),
}))

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  redirectToLogin: vi.fn(),
}))

import LifecycleMapList from '../views/lifecycle-map/LifecycleMapList.vue'

const routerPushMock = vi.fn()

function okJson(data: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => data,
  } as unknown as Response
}

const summaryMap = {
  id: 'map-1',
  name: 'Launch Flow',
  description: 'Delivery pipeline',
  owner: 'alice',
  owner_team_id: null,
  stage_count: 3,
  graduated_count: 1,
  current_version: 2,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      views: {
        LifecycleMapList: {
          edit: 'Edit',
          create_lifecycle_map: 'Create Lifecycle Map',
          name: 'Name',
          description: 'Description',
        },
      },
    },
  },
})

beforeEach(() => {
  setActivePinia(createPinia())
  routerPushMock.mockClear()
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [summaryMap] }))))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('LifecycleMapList', () => {
  it('routes to the editor when the card edit action is clicked', async () => {
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          ErrorAlert: true,
          EmptyState: true,
          Button: true,
        },
      },
    })
    await flushPromises()

    const editBtn = wrapper.find('[data-testid="lifecycle-map-list-edit"]')
    expect(editBtn.exists()).toBe(true)

    await editBtn.trigger('click')
    expect(routerPushMock).toHaveBeenCalledWith({ name: 'lifecycle-map-editor', params: { id: 'map-1' } })
  })

  it('does not trigger openMap when the card edit action is clicked', async () => {
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          ErrorAlert: true,
          EmptyState: true,
          Button: true,
        },
      },
    })
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-list-edit"]').trigger('click')
    expect(routerPushMock).not.toHaveBeenCalledWith('/lifecycle-maps/map-1')
  })

  it('opens the map detail when the card itself is clicked', async () => {
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          ErrorAlert: true,
          EmptyState: true,
          Button: true,
        },
      },
    })
    await flushPromises()

    const card = wrapper.find('[data-testid="lifecycle-map-list-card"]')
    expect(card.exists()).toBe(true)

    await card.trigger('click')
    expect(routerPushMock).toHaveBeenCalledWith('/lifecycle-maps/map-1')
  })
})
