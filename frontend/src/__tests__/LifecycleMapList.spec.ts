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

describe('LifecycleMapList responsive layout (FAR-635)', () => {
  // PageHeader and FilterBar are mounted for real here: the responsive
  // stacking contract itself lives in PageHeader (FAR-627) and is covered by
  // PageHeader.spec.ts — these tests only assert this view composes its
  // controls through that established structure.
  function mountList() {
    return mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          ErrorAlert: true,
          EmptyState: true,
          Button: true,
        },
      },
    })
  }

  it('renders the filter bar and New Map action inside the PageHeader right slot', async () => {
    const wrapper = mountList()
    await flushPromises()

    const searchInput = wrapper.find('[data-testid="filter-bar-search"]')
    expect(searchInput.exists()).toBe(true)
    const newBtn = wrapper.find('[data-testid="lifecycle-map-list-new"]')
    expect(newBtn.exists()).toBe(true)

    const headerEl = searchInput.element.closest('header')
    expect(headerEl).not.toBeNull()
    expect(headerEl?.contains(newBtn.element)).toBe(true)

    const rightSlot = newBtn.element.parentElement
    expect(rightSlot).not.toBeNull()
    expect(rightSlot?.contains(searchInput.element)).toBe(true)
  })

  it('does not hand-roll the responsive header container at page level', async () => {
    const wrapper = mountList()
    await flushPromises()

    const bandWrapper = wrapper.find('header > div')
    expect(bandWrapper.exists()).toBe(true)
    const classes = bandWrapper.classes()
    expect(classes).not.toContain('flex-col')
    expect(classes).not.toContain('sm:flex-row')
    expect(classes).not.toContain('sm:items-center')
    expect(classes).not.toContain('sm:justify-between')
  })
})
