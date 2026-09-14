import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick as vueNextTick } from 'vue'

// Restore the REAL vue-router: the shared vitest setup (src/__tests__/setup.ts)
// mocks 'vue-router' with a stub router whose currentRoute is pinned to a
// no-match route. This spec asserts on the loaded collection and error state,
// which depend on the real useRoute()/navigation (same override pattern as
// routerFeatureFlagGuard.spec.ts / demo-handoff.spec.ts).
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

async function nextTick() {
  await vueNextTick()
  await flushPromises()
}

const getMock = vi.fn()
const postMock = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(async (...args: unknown[]) => ({ data: await getMock(...args), error: undefined })),
    POST: vi.fn(async (...args: unknown[]) => ({ data: await postMock(...args), error: undefined })),
    PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
}))

import { api } from '../lib/api/client'
import CollectionDetailView from '../views/CollectionDetailView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/library', name: 'library', component: { template: '<div/>' } },
    { path: '/library/collections/:id', name: 'library-collection-detail', component: CollectionDetailView },
  ],
})

function makeCollection(overrides: Record<string, unknown> = {}) {
  return {
    id: 'col-1',
    name: 'My Collection',
    slug: 'my-collection',
    status: 'draft',
    description: 'A bundle of primitives',
    manifest_pins: [{ slug: 'my-schema', version: '1.0' }],
    ...overrides,
  }
}

describe('CollectionDetailView (FAR-760)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getMock.mockResolvedValue(makeCollection())
    postMock.mockResolvedValue({ status: 'published' })
  })

  it('loads and renders a draft collection with its pins', async () => {
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })

    // While loading, shows the loading state.
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('My Collection')
    expect(wrapper.text()).toContain('my-schema@1.0')
    // Draft status exposes the publish action.
    expect(wrapper.find('[data-testid="collection-publish"]').exists()).toBe(true)
  })

  it('renders an error when the collection cannot be loaded', async () => {
    getMock.mockResolvedValue(undefined)
    router.push('/library/collections/missing')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
  })

  it('publishes a draft collection and reflects the published status', async () => {
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="collection-publish"]').trigger('click')
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(postMock).toHaveBeenCalledTimes(1)
    const [path] = postMock.mock.calls[0] as [string, ...unknown[]]
    expect(path).toContain('/publish')

    // Status flips to published: the publish action is only shown for drafts.
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="collection-publish"]').exists()).toBe(false)
    })
  })

  it('shows a publish error when the API rejects', async () => {
    postMock.mockResolvedValue(undefined)
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="collection-publish"]').trigger('click')
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="collection-publish"]').exists()).toBe(true)
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
  })
})

describe('CollectionDetailView install action (FAR-826)', () => {
  function makeInstall() {
    return {
      install_id: 'install-1',
      collection_id: 'col-1',
      collection_version: '1.0',
      organisation_id: 'org-1',
      status: 'installed',
      community_sourced: false,
      agents_granted: false,
      resolved_manifest: null,
      connector_checklist: null,
      installed_entities: null,
      runnable: true,
      created_at: '2026-09-13T00:00:00Z',
    }
  }

  function mockPublishedCollection(installItems: unknown[] = []) {
    getMock.mockImplementation(async (path: string) => {
      if (String(path).includes('/installs')) return { items: installItems }
      return makeCollection({ status: 'published' })
    })
  }

  beforeEach(() => {
    vi.clearAllMocks()
    getMock.mockImplementation(makeCollection)
    postMock.mockResolvedValue({ status: 'published' })
  })

  it('does not offer the Install action for a draft collection', async () => {
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()
    expect(wrapper.find('[data-testid="collection-install"]').exists()).toBe(false)
  })

  it('offers the Install action for a published collection', async () => {
    mockPublishedCollection()
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()
    const button = wrapper.find('[data-testid="collection-install"]')
    expect(button.exists()).toBe(true)
    expect(button.attributes('aria-label') || button.text()).toBeTruthy()
  })

  it('POSTs to the install endpoint with the primitive id and refetches installs', async () => {
    mockPublishedCollection([])
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    // After install succeeds, the refetched installs list renders the record.
    getMock.mockImplementation(async (path: string) => {
      if (String(path).includes('/installs')) return { items: [makeInstall()] }
      return makeCollection({ status: 'published' })
    })

    await wrapper.find('[data-testid="collection-install"]').trigger('click')
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(postMock).toHaveBeenCalledTimes(1)
    const [installPath, installOpts] = postMock.mock.calls[0] as [string, { params: { path: { primitive_id: string } } }]
    expect(installPath).toBe('/api/v1/libraries/collections/{primitive_id}/install')
    expect(installOpts.params.path.primitive_id).toBe('col-1')

    // Installs list was refetched and renders the new install.
    const installsGetCalls = getMock.mock.calls.filter((c: unknown[]) => String(c[0]).includes('/installs'))
    expect(installsGetCalls.length).toBeGreaterThanOrEqual(2)
    expect(wrapper.text()).toContain('v1.0')
  })

  it('disables the Install button while the request is in flight', async () => {
    mockPublishedCollection([])
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    let resolvePost: (value: { data: unknown; error: undefined }) => void = () => {}
    vi.mocked(api.POST).mockImplementationOnce(
      (() =>
        new Promise<{ data: unknown; error: undefined }>((resolve) => {
          resolvePost = resolve
        })) as never,
    )
    const click = wrapper.find('[data-testid="collection-install"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="collection-install"]').attributes('disabled')).toBeDefined()
    resolvePost({ data: { status: 'installed' }, error: undefined })
    await click
    await flushPromises()
  })

  it('renders an install error via the alert region when the API rejects', async () => {
    mockPublishedCollection([])
    vi.mocked(api.POST).mockResolvedValueOnce({
      data: undefined,
      error: 'Forbidden',
    } as never)
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="collection-install"]').trigger('click')
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="collection-install-error"][role="alert"]').exists()).toBe(true)
  })
})
