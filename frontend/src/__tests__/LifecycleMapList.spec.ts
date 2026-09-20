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

describe('LifecycleMapList loading state', () => {
  it('shows skeleton cards while loading', async () => {
    // Stub fetch to never resolve so loading stays true
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
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
    await wrapper.vm.$nextTick()

    // Loading state shows 6 skeleton cards (animate-pulse divs)
    const skeletons = wrapper.findAll('.animate-pulse')
    expect(skeletons.length).toBe(6)
  })
})

describe('LifecycleMapList error state', () => {
  it('shows ErrorAlert when store has an error', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: async () => ({ detail: 'Server error' }),
    })))
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          EmptyState: true,
          Button: true,
        },
      },
    })
    await flushPromises()

    const errorAlert = wrapper.findComponent({ name: 'ErrorAlert' })
    expect(errorAlert.exists()).toBe(true)
  })
})

describe('LifecycleMapList search filtering', () => {
  it('filters maps by name when search changes', async () => {
    const maps = [
      { ...summaryMap, id: 'map-1', name: 'Launch Flow' },
      { ...summaryMap, id: 'map-2', name: 'Deploy Pipeline' },
    ]
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: maps }))))

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

    // Both maps visible initially
    expect(wrapper.findAll('[data-testid="lifecycle-map-list-card"]').length).toBe(2)

    // Simulate search update via the component's search ref
    const vm = wrapper.vm as unknown as { search: string }
    vm.search = 'Launch'
    await wrapper.vm.$nextTick()

    // Only Launch Flow should be visible
    const cards = wrapper.findAll('[data-testid="lifecycle-map-list-card"]')
    expect(cards.length).toBe(1)
    expect(cards[0].text()).toContain('Launch Flow')
  })

  it('filters maps by description', async () => {
    const maps = [
      { ...summaryMap, id: 'map-1', name: 'Flow A', description: 'CI/CD pipeline' },
      { ...summaryMap, id: 'map-2', name: 'Flow B', description: 'Release process' },
    ]
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: maps }))))

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

    const vm = wrapper.vm as unknown as { search: string }
    vm.search = 'Release'
    await wrapper.vm.$nextTick()

    const cards = wrapper.findAll('[data-testid="lifecycle-map-list-card"]')
    expect(cards.length).toBe(1)
    expect(cards[0].text()).toContain('Flow B')
  })

  it('shows empty state when search matches nothing', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [summaryMap] }))))
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          ErrorAlert: true,
          Button: true,
        },
      },
    })
    await flushPromises()

    const vm = wrapper.vm as unknown as { search: string }
    vm.search = 'nonexistent'
    await wrapper.vm.$nextTick()

    const emptyState = wrapper.findComponent({ name: 'EmptyState' })
    expect(emptyState.exists()).toBe(true)
  })

})

describe('LifecycleMapList owner filtering', () => {
  it('filters maps by owner when owner filter is set', async () => {
    const maps = [
      { ...summaryMap, id: 'map-1', name: 'Map A', owner: 'alice' },
      { ...summaryMap, id: 'map-2', name: 'Map B', owner: 'bob' },
      { ...summaryMap, id: 'map-3', name: 'Map C', owner: 'alice' },
    ]
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: maps }))))

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

    const vm = wrapper.vm as unknown as { ownerFilter: string }
    vm.ownerFilter = 'alice'
    await wrapper.vm.$nextTick()

    const cards = wrapper.findAll('[data-testid="lifecycle-map-list-card"]')
    expect(cards.length).toBe(2)
  })

  it('computes unique owners from loaded maps', async () => {
    const maps = [
      { ...summaryMap, id: 'map-1', owner: 'alice' },
      { ...summaryMap, id: 'map-2', owner: 'bob' },
      { ...summaryMap, id: 'map-3', owner: 'alice' },
      { ...summaryMap, id: 'map-4', owner: null },
    ]
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: maps }))))

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

    const vm = wrapper.vm as unknown as { uniqueOwners: string[] }
    expect(vm.uniqueOwners).toEqual(['alice', 'bob'])
  })
})

describe('LifecycleMapList pagination', () => {
  it('paginates maps with pageSize of 12', async () => {
    // Create 15 maps
    const maps = Array.from({ length: 15 }, (_, i) => ({
      ...summaryMap,
      id: `map-${i}`,
      name: `Map ${i}`,
    }))
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: maps }))))

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

    // First page: 12 cards
    expect(wrapper.findAll('[data-testid="lifecycle-map-list-card"]').length).toBe(12)

    // Pagination controls visible
    const nextBtn = wrapper.find('[data-testid="lifecycle-map-list-next-page"]')
    expect(nextBtn.exists()).toBe(true)

    // Navigate to page 2
    await nextBtn.trigger('click')
    await wrapper.vm.$nextTick()

    // Second page: 3 remaining cards
    expect(wrapper.findAll('[data-testid="lifecycle-map-list-card"]').length).toBe(3)
  })

  it('disables Previous button on first page', async () => {
    const maps = Array.from({ length: 15 }, (_, i) => ({
      ...summaryMap,
      id: `map-${i}`,
      name: `Map ${i}`,
    }))
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: maps }))))
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

    const prevBtn = wrapper.find('[data-testid="lifecycle-map-list-prev-page"]')
    expect(prevBtn.exists()).toBe(true)
    expect(prevBtn.attributes('disabled')).toBeDefined()
  })

  it('disables Next button on last page', async () => {
    const maps = Array.from({ length: 15 }, (_, i) => ({
      ...summaryMap,
      id: `map-${i}`,
      name: `Map ${i}`,
    }))
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: maps }))))
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

    // Navigate to page 2 (last page for 15 maps, pageSize=12)
    const nextBtn = wrapper.find('[data-testid="lifecycle-map-list-next-page"]')
    await nextBtn.trigger('click')
    await wrapper.vm.$nextTick()

    const nextBtnPage2 = wrapper.find('[data-testid="lifecycle-map-list-next-page"]')
    expect(nextBtnPage2.attributes('disabled')).toBeDefined()
  })

  it('shows page X of Y text', async () => {
    const maps = Array.from({ length: 25 }, (_, i) => ({
      ...summaryMap,
      id: `map-${i}`,
      name: `Map ${i}`,
    }))
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: maps }))))

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

    expect(wrapper.text()).toContain('Page 1 of 3')
  })

  it('navigates back with Previous button', async () => {
    const maps = Array.from({ length: 15 }, (_, i) => ({
      ...summaryMap,
      id: `map-${i}`,
      name: `Map ${i}`,
    }))
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: maps }))))

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

    // Go to page 2
    await wrapper.find('[data-testid="lifecycle-map-list-next-page"]').trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('Page 2 of 2')

    // Go back to page 1
    await wrapper.find('[data-testid="lifecycle-map-list-prev-page"]').trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('Page 1 of 2')
  })
})

describe('LifecycleMapList create dialog', () => {
  it('opens create dialog when New Map button is clicked', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [] }))))
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          ErrorAlert: true,
          Button: true,
        },
      },
    })
    await flushPromises()

    const newBtn = wrapper.find('[data-testid="lifecycle-map-list-empty-new"]')
    await newBtn.trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('Create Lifecycle Map')
    expect(wrapper.text()).toContain('Cancel')
  })

  it('closes create dialog when Cancel is clicked', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [] }))))
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          ErrorAlert: true,
          Button: true,
        },
      },
    })
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-list-empty-new"]').trigger('click')
    await wrapper.vm.$nextTick()

    // Find the Cancel button in the dialog
    const cancelBtn = wrapper.findAll('button').find((b) => b.text().trim() === 'Cancel')
    expect(cancelBtn).toBeDefined()
    await cancelBtn!.trigger('click')
    await wrapper.vm.$nextTick()

    // Dialog should be closed (no longer shows "Create Lifecycle Map")
    expect(wrapper.text()).not.toContain('Create Lifecycle Map')
  })

  it('shows error when create fails', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(okJson({ items: [] })) // initial fetchMaps
      .mockResolvedValueOnce({ // POST fails
        ok: false,
        status: 422,
        statusText: 'Unprocessable Entity',
        json: async () => ({ detail: 'Validation error' }),
      })
    vi.stubGlobal('fetch', fetchMock)

    // Use a Button stub that renders slot content so text-based queries work
    const ButtonStub = {
      template: '<button :disabled="disabled" @click="$emit(\'click\', $event)"><slot /></button>',
      props: ['disabled'],
    }
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          ErrorAlert: true,
          Button: ButtonStub,
        },
      },
    })
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-list-empty-new"]').trigger('click')
    await wrapper.vm.$nextTick()

    // Fill in the name
    const nameInput = wrapper.find('input[placeholder="My Delivery Lifecycle"]')
    await nameInput.setValue('Test Map')

    // Click Create
    const createBtn = wrapper.findAll('button').find((b) => b.text().trim() === 'Create')
    expect(createBtn).toBeDefined()
    await createBtn!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Validation error')
  })
})

describe('LifecycleMapList card content', () => {
  it('displays map name, description, stage count, and graduated count', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [summaryMap] }))))
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
    expect(card.text()).toContain('Launch Flow')
    expect(card.text()).toContain('Delivery pipeline')
    expect(card.text()).toContain('3 stages')
    expect(card.text()).toContain('1 graduated')
  })

  it('displays version badge', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [summaryMap] }))))
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
    expect(card.text()).toContain('v2')
  })

  it('displays owner name', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [summaryMap] }))))
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
    expect(card.text()).toContain('alice')
  })

  it('does not show graduated count when 0', async () => {
    const mapNoGrad = { ...summaryMap, graduated_count: 0 }
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [mapNoGrad] }))))
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
    expect(card.text()).not.toContain('graduated')
  })

  it('shows empty description placeholder when description is null', async () => {
    const mapNoDesc = { ...summaryMap, description: null }
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [mapNoDesc] }))))
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

    // Should still render the card without error
    const card = wrapper.find('[data-testid="lifecycle-map-list-card"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('Launch Flow')
  })

  it('supports keyboard navigation on card (Enter key)', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [summaryMap] }))))
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
    await card.trigger('keydown.enter')
    expect(routerPushMock).toHaveBeenCalledWith('/lifecycle-maps/map-1')
  })

  it('supports keyboard navigation on card (Space key)', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [summaryMap] }))))
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
    await card.trigger('keydown.space')
    expect(routerPushMock).toHaveBeenCalledWith('/lifecycle-maps/map-1')
  })
})

describe('LifecycleMapList query param auto-open', () => {
  it('opens create dialog when ?create=true is in the route query', async () => {
    vi.mock('vue-router', () => ({
      useRoute: vi.fn(() => ({ params: {}, query: { create: 'true' }, meta: {}, name: 'lifecycle-maps' })),
      useRouter: vi.fn(() => ({ push: routerPushMock })),
    }))

    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(okJson({ items: [] }))))
    const wrapper = mount(LifecycleMapList, {
      global: {
        plugins: [i18n],
        stubs: {
          PageHeader: true,
          FilterBar: true,
          ErrorAlert: true,
          Button: true,
        },
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('Create Lifecycle Map')
  })
})
