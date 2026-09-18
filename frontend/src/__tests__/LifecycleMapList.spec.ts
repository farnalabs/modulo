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
  exitToLogin: vi.fn(),
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

  it('gives the empty-state Create Map button the responsive width classes', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [] }))))
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          ErrorAlert: true,
        },
      },
    })
    await flushPromises()

    const emptyBtn = wrapper.find('[data-testid="lifecycle-map-list-empty-new"]')
    expect(emptyBtn.exists()).toBe(true)
    expect(emptyBtn.classes()).toEqual(expect.arrayContaining(['w-full', 'sm:w-auto']))
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

    // The controls must live inside PageHeader's own responsive <header>
    // (class "flex flex-col sm:flex-row ..."), not the page-level band header.
    // On the pre-fix flat layout the search input sat in the band header, whose
    // class never contained "flex-col", so this guards the composition.
    const pageHeader = searchInput.element.closest('header') as HTMLElement | null
    expect(pageHeader).not.toBeNull()
    expect(pageHeader?.className).toContain('flex-col')
    expect(pageHeader?.contains(newBtn.element)).toBe(true)

    // ... and inside PageHeader's #right flex-wrap container (class
    // "flex flex-wrap sm:flex-nowrap ..."). The old hand-rolled container used
    // unprefixed "items-center justify-between", so this also fails pre-fix.
    const rightSlot = newBtn.element.parentElement
    expect(rightSlot).not.toBeNull()
    expect(rightSlot?.className).toContain('sm:flex-nowrap')
    expect(rightSlot?.contains(searchInput.element)).toBe(true)
  })

  it('composes the responsive header via PageHeader (no hand-rolled responsive container)', async () => {
    const wrapper = mountList()
    await flushPromises()

    // The band-level container directly under the page header must not be a
    // hand-rolled horizontal flex row — the responsive composition is delegated
    // to PageHeader's nested <header>, not duplicated here. On the pre-fix
    // layout this div was "mx-auto flex items-center justify-between gap-3",
    // so the assertions below fail without the fix.
    const bandWrapper = wrapper.find('header.bg-card > div')
    expect(bandWrapper.exists()).toBe(true)
    const classes = bandWrapper.classes()
    expect(classes).not.toContain('flex')
    expect(classes).not.toContain('items-center')
    expect(classes).not.toContain('justify-between')
  })
})
