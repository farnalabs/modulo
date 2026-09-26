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
    expect(wrapper.find('[data-testid="collection-install-warnings"]').exists()).toBe(false)
  })

  it('renders re-provision warnings returned by a successful install', async () => {
    mockPublishedCollection([])
    const warning =
      '2 credential(s) were not exported with this bundle; re-provision them on this instance: node.env.API_KEY, node.env.OTHER_KEY.'
    postMock.mockResolvedValue({
      ...makeInstall(),
      resolved_manifest: { warnings: [warning] },
    })
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="collection-install"]').trigger('click')
    await nextTick()
    await flushPromises()
    await nextTick()

    const region = wrapper.find('[data-testid="collection-install-warnings"]')
    expect(region.exists()).toBe(true)
    // The styled block now renders inside the always-mounted polite live
    // region, so its politeness is inherited from the container.
    expect(region.attributes('aria-live')).toBeUndefined()
    expect(region.attributes('role')).toBeUndefined()
    expect(
      wrapper
        .find('[data-testid="collection-install-warnings-live"]')
        .element.contains(region.element as HTMLElement),
    ).toBe(true)
    expect(region.text()).toContain('This collection installed with warnings')
    expect(region.text()).toContain('Review each warning below')
    expect(region.text()).toContain(warning)
  })

  it('renders warnings from the refetched install so a reload keeps them visible', async () => {
    const warning =
      "2 credential(s) were not exported with this bundle; re-provision them on this instance: node.env.API_KEY, node.env.OTHER_KEY."
    mockPublishedCollection([
      { ...makeInstall(), resolved_manifest: { warnings: [warning] } },
    ])
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    const region = wrapper.find('[data-testid="collection-install-warnings"]')
    expect(postMock).not.toHaveBeenCalled()
    expect(region.exists()).toBe(true)
    expect(region.text()).toContain(warning)
  })

  it('keeps the standing install warnings when a later install attempt fails', async () => {
    const warning =
      "1 credential(s) were not exported with this bundle; re-provision them on this instance: node.env.API_KEY."
    mockPublishedCollection([
      { ...makeInstall(), resolved_manifest: { warnings: [warning] } },
    ])
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

    expect(wrapper.find('[data-testid="collection-install-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="collection-install-warnings"]').text()).toContain(warning)
  })

  it('does not render the warning region when warnings are empty', async () => {
    mockPublishedCollection([])
    postMock.mockResolvedValue({
      ...makeInstall(),
      resolved_manifest: { warnings: [] },
    })
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="collection-install"]').trigger('click')
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="collection-install-warnings"]').exists()).toBe(false)
  })

  it.each([
    ['resolved_manifest is absent', undefined],
    ['resolved_manifest has no warnings key', {}],
    ['warnings is not an array', { warnings: 'credential(s) stripped' }],
    ['warnings holds no usable strings', { warnings: [42, null, '   '] }],
  ])('renders nothing when the install payload is malformed: %s', async (_label, resolved_manifest) => {
    mockPublishedCollection([{ ...makeInstall(), resolved_manifest }])
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="collection-install-warnings"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="collection-install"]').exists()).toBe(true)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('escapes warning markup instead of rendering it as HTML', async () => {
    const warning = '<img src=x onerror="alert(1)"> credential node.env.API_KEY was not exported.'
    mockPublishedCollection([
      { ...makeInstall(), resolved_manifest: { warnings: [warning] } },
    ])
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    const region = wrapper.find('[data-testid="collection-install-warnings"]')
    expect(region.find('img').exists()).toBe(false)
    expect(region.text()).toContain(warning)
  })

  it('shows the newest install record warnings when the list carries more than one', async () => {
    const older = '1 credential(s) were not exported with this bundle; re-provision them on this instance: node.env.OLD_KEY.'
    const newer = '1 credential(s) were not exported with this bundle; re-provision them on this instance: node.env.NEW_KEY.'
    mockPublishedCollection([
      { ...makeInstall(), created_at: '2026-09-14T00:00:00Z', resolved_manifest: { warnings: [newer] } },
      { ...makeInstall(), created_at: '2026-09-13T00:00:00Z', resolved_manifest: { warnings: [older] } },
    ])
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    const region = wrapper.find('[data-testid="collection-install-warnings"]')
    expect(region.text()).toContain(newer)
    expect(region.text()).not.toContain(older)
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


// WCAG 4.1.3 (Status Messages): a status message must be announced without
// moving focus. An aria-live region that enters the DOM in the same mutation
// as its own content is not reliably announced, so the announcing container
// has to pre-exist and stay empty while the install is still in flight — only
// then can the re-provision warning reach a screen-reader user who never looks
// at the top of the page.
describe('CollectionDetailView install warning announcement (FAR-1231)', () => {
  const LIVE_REGION = '[data-testid="collection-install-warnings-live"]'
  const WARNING_REGION = '[data-testid="collection-install-warnings"]'

  beforeEach(() => {
    vi.clearAllMocks()
    postMock.mockResolvedValue({ status: 'published' })
  })

  it('mounts the polite live region empty before an install is attempted', async () => {
    mockPublishedCollection([])
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    const live = wrapper.find(LIVE_REGION)
    expect(live.exists()).toBe(true)
    expect(live.attributes('aria-live')).toBe('polite')
    // Nothing rendered yet, and nothing in the accessibility tree removed.
    expect(live.element.children.length).toBe(0)
    expect(live.text()).toBe('')
    expect(live.attributes('hidden')).toBeUndefined()
    expect(live.attributes('aria-hidden')).toBeUndefined()
    expect(wrapper.find(WARNING_REGION).exists()).toBe(false)
  })

  it('keeps the empty live region exposed to assistive tech', async () => {
    mockPublishedCollection([])
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    // The empty container must be out of flow WITHOUT leaving the a11y tree.
    // `empty:hidden` compiles to `display: none`, and no screen reader
    // announces a live region that was display:none when it populated — which
    // would reinstate the exact bug this change exists to fix. `empty:sr-only`
    // is position:absolute (no phantom space-y-4 gap) but still exposed.
    //
    // Asserted on the class rather than computed style: vite.config.ts
    // deliberately injects only the two JsonViewer stylesheets into jsdom
    // (test.css.include), so Tailwind output is not available here.
    const live = wrapper.find(LIVE_REGION)
    expect(live.classes()).toContain('empty:sr-only')
    expect(live.classes()).not.toContain('empty:hidden')
    expect(live.classes()).not.toContain('hidden')
  })

  it('adds the warning text to the already-mounted live region after install', async () => {
    mockPublishedCollection([])
    const warning =
      '2 credential(s) were not exported with this bundle; re-provision them on this instance: node.env.API_KEY, node.env.OTHER_KEY.'
    postMock.mockResolvedValue({
      ...makeInstall(),
      resolved_manifest: { warnings: [warning] },
    })
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    const live = wrapper.find(LIVE_REGION)
    expect(live.element.children.length).toBe(0)

    await wrapper.find('[data-testid="collection-install"]').trigger('click')
    await nextTick()
    await flushPromises()
    await nextTick()

    // Same DOM node throughout: content was inserted into a live region that
    // was already on the page, which is what gets announced.
    expect(wrapper.find(LIVE_REGION).element).toBe(live.element)
    expect(wrapper.find(WARNING_REGION).exists()).toBe(true)
    expect(wrapper.find(LIVE_REGION).text()).toContain(warning)
    // The element is no longer `:empty`, so the sr-only rule stops matching
    // and the warning box lays out normally.
  })

  it('leaves the live region empty when the installed bundle stripped nothing', async () => {
    mockPublishedCollection([])
    postMock.mockResolvedValue({ ...makeInstall(), resolved_manifest: { warnings: [] } })
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="collection-install"]').trigger('click')
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.find(WARNING_REGION).exists()).toBe(false)
    expect(wrapper.find(LIVE_REGION).element.children.length).toBe(0)
  })

  it('does not report an install failure when only the follow-up refetch fails', async () => {
    const warning =
      '1 credential(s) were not exported with this bundle; re-provision them on this instance: node.env.API_KEY.'
    // The mount-time installs GET succeeds; the refetch after the install POST
    // rejects the way api.GET does on a transport failure.
    let installListCalls = 0
    getMock.mockImplementation(async (path: string) => {
      if (String(path).includes('/installs')) {
        installListCalls += 1
        if (installListCalls > 1) throw new Error('network down')
        return { items: [] }
      }
      return makeCollection({ status: 'published' })
    })
    postMock.mockResolvedValue({ ...makeInstall(), resolved_manifest: { warnings: [warning] } })
    router.push('/library/collections/col-1')
    await router.isReady()
    const wrapper = mount(CollectionDetailView, { global: { plugins: [router] } })
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="collection-install"]').trigger('click')
    await nextTick()
    await flushPromises()
    await nextTick()

    // The install itself succeeded, so a red "install failed" would contradict
    // the re-provision warning sitting right above it.
    expect(wrapper.find('[data-testid="collection-install-error"]').exists()).toBe(false)
    expect(wrapper.find(WARNING_REGION).text()).toContain(warning)
  })
})
