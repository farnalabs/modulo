import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory, useRoute } from 'vue-router'
import { nextTick as vueNextTick, reactive } from 'vue'

async function nextTick() {
  await vueNextTick()
  await flushPromises()
}

const getMock = vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })
const postMock = vi.fn()
const patchMock = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(async (...args: unknown[]) => ({ data: await getMock(...args), error: undefined })),
    POST: vi.fn(async (...args: unknown[]) => ({ data: await postMock(...args), error: undefined })),
    PATCH: vi.fn(async (...args: unknown[]) => ({ data: await patchMock(...args), error: undefined })),
  },
}))

import LibraryView from '../views/LibraryView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/library', name: 'library', component: LibraryView },
    { path: '/library/:id/create-pipeline', name: 'library-pipeline-wizard', component: { template: '<div/>' } },
    { path: '/library/collections/new', name: 'library-collection-create', component: { template: '<div/>' } },
    { path: '/library/collections/:id', name: 'library-collection-detail', component: { template: '<div/>' } },

  ],
})

const NATIVE_ITEM = {
  id: 'native-1',
  organisation_id: 'org-1',
  source: 'modulo',
  primitive_type: 'workflow',
  name: 'PRD to Requirements',
  slug: 'prd-to-requirements',
  description: null,
  author: 'modulo',
  version: '1.0',
  tags: [],
  visibility: 'community',
  forked_from: null,
  auto_update: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
}

const COMMUNITY_ITEM = {
  id: 'community-1',
  organisation_id: 'org-1',
  source: 'community',
  primitive_type: 'workflow',
  name: 'Translate to French',
  slug: 'translate-to-french',
  description: null,
  author: 'community',
  version: '1.0',
  tags: [],
  visibility: 'community',
  forked_from: null,
  auto_update: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
}

describe('LibraryView', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    getMock.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })
    await router.push('/library')
    await router.isReady()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders without crashing', async () => {
    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Library')
  })

  it('segregates preview primitives into a disclosure section and hides in-dev primitives', async () => {
    getMock.mockResolvedValue({
      items: [
        { id: 'native-1', organisation_id: 'org', source: 'local', primitive_type: 'workflow', name: 'Native Workflow', slug: 'native-workflow', description: null, author: 'a', version: '1.0', tags: [], visibility: 'org', forked_from: null, auto_update: true, tier: 'native', created_at: '', updated_at: '' },
        { id: 'preview-1', organisation_id: 'org', source: 'local', primitive_type: 'workflow', name: 'Preview Workflow', slug: 'preview-workflow', description: null, author: 'a', version: '1.0', tags: [], visibility: 'org', forked_from: null, auto_update: true, tier: 'preview', created_at: '', updated_at: '' },
        { id: 'indev-1', organisation_id: 'org', source: 'local', primitive_type: 'workflow', name: 'InDev Workflow', slug: 'indev-workflow', description: null, author: 'a', version: '1.0', tags: [], visibility: 'org', forked_from: null, auto_update: true, tier: 'in_dev', created_at: '', updated_at: '' },
      ],
      total: 3,
      page: 1,
      page_size: 12,
    })

    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Native Workflow')
    expect(wrapper.text()).not.toContain('InDev Workflow')

    const previewSection = wrapper.find('[data-testid="library-preview-section"]')
    expect(previewSection.exists()).toBe(true)
    expect(previewSection.text()).toContain('Preview Workflow')
  })

  it('renders the Native section by default, excluding community items even if returned mixed', async () => {
    getMock.mockResolvedValue({ items: [NATIVE_ITEM, COMMUNITY_ITEM], total: 2, page: 1, page_size: 12 })
    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('PRD to Requirements')
    expect(wrapper.text()).not.toContain('Translate to French')
  })

  it('renders the Community section separately with a non-verified indicator', async () => {
    getMock.mockResolvedValue({ items: [COMMUNITY_ITEM], total: 1, page: 1, page_size: 12 })
    await router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()

    await wrapper.get('[data-testid="library-section-community"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Translate to French')
    expect(wrapper.find('[data-testid="library-community-badge"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="library-community-badge"]').text()).toContain('not verified')
    expect(wrapper.find('[data-testid="library-community-disclaimer"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('PRD to Requirements')
  })

  it('sends a combined primitive_types filter when multiple types are selected', async () => {
    getMock.mockResolvedValue({ items: [NATIVE_ITEM], total: 1, page: 1, page_size: 12 })
    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()

    const openDropdown = async () => {
      await wrapper.get('[data-testid="library-type-filter-button"]').trigger('click')
      await nextTick()
    }
    const checkboxAt = (index: number) => {
      const checkbox = wrapper.findAll('[data-testid="library-type-filter-dropdown"] input')[index]
      if (!checkbox) throw new Error(`expected a type checkbox at index ${index}`)
      return checkbox
    }

    await openDropdown()
    await checkboxAt(1).trigger('change') // workflow
    await nextTick()
    await openDropdown()
    await checkboxAt(2).trigger('change') // agent
    await nextTick()

    const lastCall = getMock.mock.calls.at(-1)!
    const opts = lastCall[1] as { params: { query: Record<string, unknown> } }
    expect(opts.params.query.primitive_types).toBe('workflow,agent')
    expect(opts.params.query.primitive_type).toBeUndefined()
  })

  it('reloads primitives with the search term after typing (debounced), resetting to page 1', async () => {
    getMock.mockResolvedValue({ items: [], total: 24, page: 1, page_size: 12 })
    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    await nextTick()

    await wrapper.get('[data-testid="library-next-page"]').trigger('click')
    await nextTick()
    await nextTick()

    let lastCall = getMock.mock.calls.at(-1)!
    let opts = lastCall[1] as { params: { query: Record<string, unknown> } }
    expect(opts.params.query.page).toBe(2)

    const searchInput = wrapper.find('[data-testid="filter-bar-search"]')
    expect(searchInput.exists()).toBe(true)

    vi.useFakeTimers()
    await searchInput.setValue('foo')
    await vueNextTick()

    lastCall = getMock.mock.calls.at(-1)!
    opts = lastCall[1] as { params: { query: Record<string, unknown> } }
    expect(opts.params.query.search).toBeUndefined()

    vi.advanceTimersByTime(300)
    vi.useRealTimers()
    await nextTick()
    await nextTick()

    lastCall = getMock.mock.calls.at(-1)!
    opts = lastCall[1] as { params: { query: Record<string, unknown> } }
    expect(opts.params.query.search).toBe('foo')
    expect(opts.params.query.page).toBe(1)
    wrapper.unmount()
  })

  it('does not render lifecycle-map primitives in the native library surface', async () => {
    getMock.mockResolvedValue({
      items: [
        { id: 'lm-prim-1', organisation_id: 'org-1', source: 'modulo', primitive_type: 'lifecycle_map', name: 'SDLC Map', slug: 'sdlc-map', description: null, author: 'modulo', version: '1.0', tags: [], visibility: 'community', forked_from: null, auto_update: true, created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z' },
      ],
      total: 1,
      page: 1,
      page_size: 12,
    })

    await router.push('/library')
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).not.toContain('SDLC Map')
    expect(wrapper.find('[data-testid="library-create-lifecycle-map"]').exists()).toBe(false)
    expect(postMock).not.toHaveBeenCalled()
  })

  it('calls toggleAutoUpdate which toggles the primitive', async () => {
    getMock.mockResolvedValue({ items: [NATIVE_ITEM], total: 1, page: 1, page_size: 12 })
    patchMock.mockResolvedValue({ ...NATIVE_ITEM, auto_update: false })

    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    await nextTick()

    // Call the internal toggleAutoUpdate function directly
    const vm = wrapper.vm as any
    await vm.toggleAutoUpdate(NATIVE_ITEM)
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(patchMock).toHaveBeenCalledWith('/api/v1/libraries/{primitive_id}', {
      params: { path: { primitive_id: 'native-1' } },
      body: { auto_update: false },
    })
    wrapper.unmount()
  })

  it('handles toggleAutoUpdate error and restores loading state', async () => {
    getMock.mockResolvedValue({ items: [NATIVE_ITEM], total: 1, page: 1, page_size: 12 })
    patchMock.mockRejectedValue(new Error('network error'))

    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    await nextTick()

    const vm = wrapper.vm as any
    await vm.toggleAutoUpdate(NATIVE_ITEM)
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('network error')
    wrapper.unmount()
  })

  it('handles a toggle response without data', async () => {
    getMock.mockResolvedValue({ items: [NATIVE_ITEM], total: 1, page: 1, page_size: 12 })
    await router.push('/library')
    const wrapper = mount(LibraryView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()
    const vm = wrapper.vm as any

    patchMock.mockResolvedValueOnce(undefined)
    await vm.toggleAutoUpdate(NATIVE_ITEM)
    expect(patchMock).toHaveBeenCalledWith('/api/v1/libraries/{primitive_id}', {
      params: { path: { primitive_id: 'native-1' } },
      body: { auto_update: false },
    })
    // An undefined response body must not replace the primitive, and the
    // per-row spinner must still be cleared.
    expect(vm.primitives.find((p: { id: string }) => p.id === 'native-1').auto_update).toBe(true)
    expect(vm.toggleLoading['native-1']).toBe(false)
    wrapper.unmount()
  })

  it('covers collection pagination and outside-click closing', async () => {
    getMock.mockResolvedValue({ items: [], total: 24, page: 1, page_size: 12 })
    await router.push('/library')
    const wrapper = mount(LibraryView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    // The type-filter dropdown opens on click and closes on an outside mousedown.
    await wrapper.get('[data-testid="library-type-filter-button"]').trigger('click')
    expect(wrapper.find('[data-testid="library-type-filter-dropdown"]').exists()).toBe(true)
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await nextTick()
    expect(wrapper.find('[data-testid="library-type-filter-dropdown"]').exists()).toBe(false)

    const pagesRequested = () =>
      getMock.mock.calls
        .map((c: unknown[]) => (c[1] as { params?: { query?: { page?: number } } })?.params?.query?.page)
        .filter((p): p is number => typeof p === 'number')

    await wrapper.get('[data-testid="library-next-page"]').trigger('click')
    await nextTick()
    await nextTick()
    expect(pagesRequested()).toContain(2)

    await wrapper.get('[data-testid="library-previous-page"]').trigger('click')
    await nextTick()
    await nextTick()
    expect(pagesRequested().slice(-1)).toEqual([1])
    wrapper.unmount()
  })

  it('navigates from primitive and collection detail actions', async () => {
    const collection = { ...NATIVE_ITEM, id: 'collection-1', primitive_type: 'library_collection' }
    getMock.mockResolvedValue({ items: [NATIVE_ITEM, collection], total: 2, page: 1, page_size: 12 })
    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()
    const vm = wrapper.vm as any
    vm.viewPrimitive(NATIVE_ITEM)
    vm.viewCollection(collection)
    expect(router.push).toHaveBeenCalledWith({ name: 'library-pipeline-wizard', params: { id: 'native-1' } })
    expect(router.push).toHaveBeenCalledWith({ name: 'library-collection-detail', params: { id: 'collection-1' } })
    wrapper.unmount()
  })
})


describe('LibraryView tab routing (FAR-2260)', () => {
  type Query = Record<string, unknown>

  // The suite mocks `vue-router` globally (src/__tests__/setup.ts), so the
  // component's `useRoute()` is driven by this reactive object instead of a
  // real history. Assigning `query` here is exactly what a tab-link
  // navigation, a deep link or a popstate does to the live route.
  const currentRoute = reactive({
    path: '/library',
    fullPath: '/library',
    params: {} as Record<string, string>,
    query: {} as Query,
    hash: '',
    matched: [],
    name: 'library',
    redirectedFrom: undefined,
  })

  function setRoute(query: Query) {
    currentRoute.path = '/library'
    currentRoute.query = query
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) {
      if (typeof value === 'string') search.set(key, value)
    }
    const qs = search.toString()
    currentRoute.fullPath = qs ? `/library?${qs}` : '/library'
  }

  function callsWhere(predicate: (query: Query) => boolean): unknown[][] {
    return getMock.mock.calls.filter((call: unknown[]) => {
      const query = (call[1] as { params?: { query?: Query } } | undefined)?.params?.query
      return !!query && predicate(query)
    })
  }

  async function mountAt(query: Query) {
    setRoute(query)
    const wrapper = mount(LibraryView, { global: { plugins: [router] } })
    await nextTick()
    return wrapper
  }

  beforeEach(() => {
    vi.clearAllMocks()
    getMock.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })
    ;(useRoute as unknown as { mockReturnValue: (route: unknown) => void }).mockReturnValue(currentRoute)
    setRoute({})
  })

  it('deep-links straight into the community section', async () => {
    getMock.mockResolvedValue({ items: [COMMUNITY_ITEM], total: 1, page: 1, page_size: 12 })
    const wrapper = await mountAt({ section: 'community' })

    expect(callsWhere((query) => query.source === 'community').length).toBeGreaterThan(0)
    expect(wrapper.get('[data-testid="library-section-community"]').classes()).toContain('active')
    expect(wrapper.find('[data-testid="library-community-disclaimer"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Translate to French')
    wrapper.unmount()
  })

  it('deep-links straight into the collections section', async () => {
    const wrapper = await mountAt({ section: 'collections' })

    expect(callsWhere((query) => query.primitive_type === 'library_collection').length).toBeGreaterThan(0)
    expect(wrapper.get('[data-testid="library-section-collections"]').classes()).toContain('active')
    expect(wrapper.find('[data-testid="library-create-collection"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('marks the native tab active on a bare /library deep link', async () => {
    getMock.mockResolvedValue({ items: [NATIVE_ITEM], total: 1, page: 1, page_size: 12 })
    const wrapper = await mountAt({})

    expect(wrapper.get('[data-testid="library-section-native"]').classes()).toContain('active')
    expect(wrapper.get('[data-testid="library-section-community"]').classes()).not.toContain('active')
    expect(wrapper.text()).toContain('PRD to Requirements')
    wrapper.unmount()
  })

  it('fetches once when a tab click is followed by its route change', async () => {
    const wrapper = await mountAt({})
    getMock.mockClear()

    // The click applies the section and fetches…
    await wrapper.get('[data-testid="library-section-community"]').trigger('click')
    await nextTick()
    // …then the router-link navigation commits the matching query, which must
    // not trigger a second fetch for the section already on screen.
    setRoute({ section: 'community' })
    await nextTick()

    expect(callsWhere((query) => query.source === 'community')).toHaveLength(1)
    wrapper.unmount()
  })

  it('follows browser back and forward, fetching each section once', async () => {
    const wrapper = await mountAt({ section: 'community' })
    expect(wrapper.get('[data-testid="library-section-community"]').classes()).toContain('active')

    // Back to the bare /library URL: the default (native) section is fetched once.
    getMock.mockClear()
    setRoute({})
    await nextTick()

    expect(callsWhere((query) => query.source === 'community')).toHaveLength(0)
    expect(callsWhere((query) => query.source === undefined)).toHaveLength(1)
    expect(wrapper.get('[data-testid="library-section-native"]').classes()).toContain('active')

    // Forward to ?section=community: fetched once, and the tab follows.
    getMock.mockClear()
    setRoute({ section: 'community' })
    await nextTick()

    expect(callsWhere((query) => query.source === 'community')).toHaveLength(1)
    expect(wrapper.get('[data-testid="library-section-community"]').classes()).toContain('active')
    wrapper.unmount()
  })
})


describe('LibraryView responsive layout (FAR-640)', () => {
  async function mountLibrary() {
    router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()
    return wrapper
  }

  it('renders the create action, filter bar, and type filter inside the PageHeader right slot', async () => {
    const wrapper = await mountLibrary()

    const createBtn = wrapper.find('[data-testid="library-create-pipeline-header"]')
    const searchInput = wrapper.find('[data-testid="filter-bar-search"]')
    const typeBtn = wrapper.find('[data-testid="library-type-filter-button"]')
    expect(createBtn.exists()).toBe(true)
    expect(searchInput.exists()).toBe(true)
    expect(typeBtn.exists()).toBe(true)

    const headerEl = createBtn.element.closest('header')
    expect(headerEl).not.toBeNull()
    expect(headerEl?.contains(searchInput.element)).toBe(true)
    expect(headerEl?.contains(typeBtn.element)).toBe(true)

    const rightSlot = createBtn.element.parentElement
    expect(rightSlot).not.toBeNull()
    expect(rightSlot?.contains(searchInput.element)).toBe(true)
    expect(rightSlot?.contains(typeBtn.element)).toBe(true)
  })

  it('keeps the type-filter dropdown anchored to its trigger inside the right slot', async () => {
    const wrapper = await mountLibrary()

    const typeBtn = wrapper.get('[data-testid="library-type-filter-button"]')
    const dropdownHost = typeBtn.element.parentElement as HTMLElement
    expect(dropdownHost.classList.contains('relative')).toBe(true)
    expect(dropdownHost.closest('header')).not.toBeNull()
  })

  it('does not hand-roll the responsive header container at page level', async () => {
    const wrapper = await mountLibrary()

    const bandWrapper = wrapper.find('header > div')
    expect(bandWrapper.exists()).toBe(true)
    const classes = bandWrapper.classes()
    expect(classes).not.toContain('flex-col')
    expect(classes).not.toContain('sm:flex-row')
    expect(classes).not.toContain('sm:items-center')
    expect(classes).not.toContain('sm:justify-between')
  })

  it('shows collections tab and create button', async () => {
    getMock.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })
    await router.push('/library')
    await router.isReady()
    const wrapper = mount(LibraryView, {
      global: { plugins: [router] },
    })
    await nextTick()

    await wrapper.get('[data-testid="library-section-collections"]').trigger('click')
    await nextTick()
    await nextTick()

    const collectionsTab = wrapper.find('[data-testid="library-section-collections"]')
    expect(collectionsTab.exists()).toBe(true)

    const createBtn = wrapper.find('[data-testid="library-create-collection"]')
    expect(createBtn.exists()).toBe(true)
  })
})
