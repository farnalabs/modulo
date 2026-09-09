import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick as vueNextTick } from 'vue'

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
