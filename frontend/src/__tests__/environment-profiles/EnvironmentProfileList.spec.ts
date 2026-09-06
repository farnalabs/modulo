import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { getMock, delMock, routerPush } = vi.hoisted(() => ({
  getMock: vi.fn(),
  delMock: vi.fn(),
  routerPush: vi.fn(),
}))

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), put: vi.fn(), delete: delMock, patch: vi.fn() }),
}))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/environment-profiles',
    fullPath: '/environment-profiles',
    params: {},
    query: {},
    hash: '',
    matched: [],
    name: null,
    redirectedFrom: undefined,
    meta: {},
  })),
  useRouter: vi.fn(() => ({ push: routerPush, replace: vi.fn() })),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(() => ({})),
}))

import EnvironmentProfileList from '../../views/environment-profiles/EnvironmentProfileList.vue'

function profile(over: Record<string, unknown> = {}) {
  return {
    id: 'prof-1',
    name: 'Python Dev',
    description: 'Python sandbox with git',
    provider_type: 'local_docker',
    image_ref: 'python:3.12-slim',
    capabilities: ['git'],
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

async function flush() {
  await flushPromises()
  await nextTick()
}

function mountList() {
  return mount(EnvironmentProfileList, {
    global: {
      mocks: { $router: { push: routerPush } },
    },
  })
}

describe('EnvironmentProfileList', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    getMock.mockResolvedValue({ items: [profile()], total: 1 })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders profile cards with name, provider and tier badge after load', async () => {
    const wrapper = mountList()
    await flush()

    expect(getMock).toHaveBeenCalledWith('/api/v1/environment-profiles')
    const names = wrapper.findAll('[data-testid="envprofile-list-name"]')
    expect(names).toHaveLength(1)
    expect(names[0].text()).toBe('Python Dev')
    expect(wrapper.text()).toContain('local_docker')
    expect(wrapper.find('[data-testid="envprofile-list-tier-badge"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="envprofile-list-tier-badge"]').text().length).toBeGreaterThan(0)
  })

  it('shows the empty state without search and its create button navigates to the form', async () => {
    getMock.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mountList()
    await flush()

    expect(wrapper.text()).toContain('No environment profiles')
    expect(wrapper.find('[data-testid="envprofile-list-empty-create"]').exists()).toBe(true)

    await wrapper.find('[data-testid="envprofile-list-empty-create"]').trigger('click')
    expect(routerPush).toHaveBeenCalledWith('/environment-profiles/new')
  })

  it('search filters cards by name and shows the no-match state', async () => {
    getMock.mockResolvedValue({
      items: [profile(), profile({ id: 'prof-2', name: 'Node Builder', provider_type: 'e2b', description: null })],
      total: 2,
    })
    const wrapper = mountList()
    await flush()

    await wrapper.find('[data-testid="envprofile-list-search"]').setValue('node')
    await nextTick()
    expect(wrapper.findAll('[data-testid="envprofile-list-name"]')).toHaveLength(1)
    expect(wrapper.text()).toContain('Node Builder')

    await wrapper.find('[data-testid="envprofile-list-search"]').setValue('zzz-no-match')
    await nextTick()
    expect(wrapper.findAll('[data-testid="envprofile-list-name"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('No profiles match')
    expect(wrapper.text()).toContain('zzz-no-match')
  })

  it('a load failure shows the inline ErrorAlert with the formatted error (the ErrorAlert Retry button is hidden repo-wide — see AdminAuditView BUG characterisation)', async () => {
    getMock.mockRejectedValue(new Error('backend down'))
    const wrapper = mountList()
    await flush()

    const alert = wrapper.find('.border-destructive\\/50')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('backend down')
    // ErrorAlert defaults `retryable` to true, so a load failure with an
    // on-retry handler (store.fetchProfiles) offers the Retry action.
    // Characterised in AdminAuditView.spec.ts (FAR-608 fix).
    expect(alert.findAll('button').filter((b) => b.text() === 'Retry')).toHaveLength(1)
    wrapper.unmount()
  })

  it('new-profile button navigates to the form', async () => {
    const wrapper = mountList()
    await flush()

    await wrapper.find('[data-testid="envprofile-list-new"]').trigger('click')
    expect(routerPush).toHaveBeenCalledWith('/environment-profiles/new')
  })

  it('test connection streams SSE events into the per-profile panel and dismisses', async () => {
    const encoder = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"event":"provisioning","detail":"booting","timestamp":"t1"}\n\n'))
        controller.enqueue(encoder.encode('data: {"event":"provisioned","detail":"ready","timestamp":"t2"}\n\n'))
        controller.close()
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, body: stream }))

    const wrapper = mountList()
    await flush()

    await wrapper.find('[data-testid="envprofile-test"]').trigger('click')
    await flush()

    const panel = wrapper.find('[data-testid="envprofile-test-panel"]')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('provisioning')
    expect(panel.text()).toContain('booting')
    expect(panel.text()).toContain('provisioned')
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      '/api/v1/environment-profiles/prof-1/test',
      expect.objectContaining({ method: 'POST' }),
    )

    await wrapper.find('[data-testid="envprofile-test-dismiss"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="envprofile-test-panel"]').exists()).toBe(false)
  })

  it('test connection HTTP failure records a failed event with the status code', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, body: null }))

    const wrapper = mountList()
    await flush()

    await wrapper.find('[data-testid="envprofile-test"]').trigger('click')
    await flush()

    const panel = wrapper.find('[data-testid="envprofile-test-panel"]')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('failed')
    expect(panel.text()).toContain('HTTP 500')
  })

  it('test connection network error records a failed event with the error message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('socket hang up')))

    const wrapper = mountList()
    await flush()

    await wrapper.find('[data-testid="envprofile-test"]').trigger('click')
    await flush()

    expect(wrapper.find('[data-testid="envprofile-test-panel"]').text()).toContain('socket hang up')
  })

  it('delete flow confirms, calls the store delete, and removes the card', async () => {
    delMock.mockResolvedValue(undefined)
    const wrapper = mountList()
    await flush()

    await wrapper.find('[data-testid="envprofile-list-delete"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Delete "Python Dev"?')

    await wrapper.find('[data-testid="envprofile-list-delete-cancel"]').trigger('click')
    await nextTick()
    expect(delMock).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Delete "Python Dev"?')

    await wrapper.find('[data-testid="envprofile-list-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="envprofile-list-delete-confirm"]').trigger('click')
    await flush()

    expect(delMock).toHaveBeenCalledWith('/api/v1/environment-profiles/prof-1')
    expect(wrapper.findAll('[data-testid="envprofile-list-name"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('No environment profiles')
  })

  it('a failed delete surfaces the delete error in the confirm panel and the store error alert replaces the grid', async () => {
    delMock.mockRejectedValue(new Error('delete forbidden'))
    const wrapper = mountList()
    await flush()

    await wrapper.find('[data-testid="envprofile-list-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="envprofile-list-delete-confirm"]').trigger('click')
    await flush()

    expect(wrapper.text()).toContain('delete forbidden')
    // The store-level error branch replaces the grid (production behaviour).
    expect(wrapper.find('.border-destructive\\/50').text()).toContain('delete forbidden')
  })
})
