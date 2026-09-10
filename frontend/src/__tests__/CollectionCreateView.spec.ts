import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick as vueNextTick } from 'vue'

// Restore the REAL vue-router: the shared vitest setup (src/__tests__/setup.ts)
// mocks 'vue-router' with a stub router whose push/currentRoute never navigate,
// so router.push({ name: 'library-collection-detail' }) would never commit. This
// spec asserts on the committed route, so it needs the real implementation
// (same override pattern as routerFeatureFlagGuard.spec.ts / demo-handoff.spec.ts).
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

async function nextTick() {
  await vueNextTick()
  await flushPromises()
}

const getMock = vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })
const postMock = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(async (...args: unknown[]) => ({ data: await getMock(...args), error: undefined })),
    POST: vi.fn(async (...args: unknown[]) => ({ data: await postMock(...args), error: undefined })),
    PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
}))

import CollectionCreateView from '../views/CollectionCreateView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/library', name: 'library', component: { template: '<div/>' } },
    { path: '/library/collections/new', name: 'library-collection-create', component: { template: '<div/>' } },
    { path: '/library/collections/:id', name: 'library-collection-detail', component: { template: '<div/>' } },
  ],
})

describe('CollectionCreateView (FAR-760)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getMock.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 12 })
    postMock.mockResolvedValue({ id: 'new-collection-id' })
  })

  it('renders the create-collection form', async () => {
    router.push('/library/collections/new')
    await router.isReady()
    const wrapper = mount(CollectionCreateView, { global: { plugins: [router] } })
    await nextTick()
    expect(wrapper.find('#collection-name').exists()).toBe(true)
    expect(wrapper.find('#collection-slug').exists()).toBe(true)
    expect(wrapper.find('[data-testid="collection-create-submit"]').exists()).toBe(true)
  })

  it('adds and removes manifest pin rows', async () => {
    router.push('/library/collections/new')
    await router.isReady()
    const wrapper = mount(CollectionCreateView, { global: { plugins: [router] } })
    await nextTick()
    expect(wrapper.findAll('[id^="pin-slug-"]')).toHaveLength(0)
    await wrapper.find('[data-testid="collection-add-pin"]').trigger('click')
    await nextTick()
    expect(wrapper.findAll('[id^="pin-slug-"]').length).toBeGreaterThan(0)
  })

  it('submits a collection and navigates to its detail page', async () => {
    router.push('/library/collections/new')
    await router.isReady()
    const wrapper = mount(CollectionCreateView, { global: { plugins: [router] } })
    await nextTick()

    await wrapper.find('#collection-name').setValue('My New Collection')
    await wrapper.find('#collection-slug').setValue('my-new-collection')
    await nextTick()

    await wrapper.find('form').trigger('submit')
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(postMock).toHaveBeenCalledTimes(1)
    const [path, opts] = postMock.mock.calls[0] as [string, { body: Record<string, unknown> }]
    expect(path).toBe('/api/v1/libraries/collections')
    expect(opts.body.name).toBe('My New Collection')
    expect(opts.body.slug).toBe('my-new-collection')

    await vi.waitFor(() => {
      expect(router.currentRoute.value.name).toBe('library-collection-detail')
    })
  })

  it('shows an error when the API rejects the create', async () => {
    postMock.mockResolvedValue(undefined)
    router.push('/library/collections/new')
    await router.isReady()
    const wrapper = mount(CollectionCreateView, { global: { plugins: [router] } })
    await nextTick()

    await wrapper.find('#collection-name').setValue('Broken')
    await wrapper.find('#collection-slug').setValue('broken')
    await nextTick()

    await wrapper.find('[data-testid="collection-create-submit"]').trigger('submit')
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(router.currentRoute.value.name).not.toBe('library-collection-detail')
  })

  it('removes a manifest pin row', async () => {
    router.push('/library/collections/new')
    await router.isReady()
    const wrapper = mount(CollectionCreateView, { global: { plugins: [router] } })
    await nextTick()
    await wrapper.find('[data-testid="collection-add-pin"]').trigger('click')
    await nextTick()
    expect(wrapper.findAll('[id^="pin-slug-"]').length).toBe(1)
    await wrapper.find('[data-testid="collection-remove-pin"]').trigger('click')
    await nextTick()
    expect(wrapper.findAll('[id^="pin-slug-"]').length).toBe(0)
  })
})
