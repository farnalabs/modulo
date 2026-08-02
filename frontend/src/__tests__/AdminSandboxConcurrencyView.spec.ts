import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { usePlanStore } from '../stores/planStore'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((path: string) => {
      if (path === '/api/v1/admin/org/sandbox-concurrency') {
        return Promise.resolve({ data: { sandbox_concurrency_limit: 5 }, error: null })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({
          data: {
            license: { tier: 'team', has_license_key: true, is_valid: true },
            flags: [{ name: 'environment_profiles', description: '', tier: 'team', currently_active: true, depends_on: null }],
            would_activate: [],
          },
          error: null,
        })
      }
      return Promise.resolve({ data: null, error: null })
    }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: null }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminSandboxConcurrencyView from '../views/AdminSandboxConcurrencyView.vue'

describe('AdminSandboxConcurrencyView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  function mountView() {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ currentTier: 'team', features: { environment_profiles: true } })
    return mount(AdminSandboxConcurrencyView, {
      global: { plugins: [pinia] },
    })
  }

  it('renders without crashing', async () => {
    const wrapper = mountView()

    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Max concurrent sandbox runs')
  })

  it('loads the current sandbox concurrency limit from the API', async () => {
    const wrapper = mountView()

    await nextTick()
    await nextTick()
    await nextTick()

    const input = wrapper.find('[data-testid="admin-sandbox-concurrency-limit"]') as any
    expect(input.element.value).toBe('5')
  })

  it('shows an empty input when the limit is null (unlimited)', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/org/sandbox-concurrency') {
        return Promise.resolve({ data: { sandbox_concurrency_limit: null }, error: null })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { license: { tier: 'team' }, flags: [], would_activate: [] }, error: null })
      }
      return Promise.resolve({ data: null, error: null })
    })

    const wrapper = mountView()

    await nextTick()
    await nextTick()
    await nextTick()

    const input = wrapper.find('[data-testid="admin-sandbox-concurrency-limit"]') as any
    expect(input.element.value).toBe('')

    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await nextTick()

    expect(api.PUT).toHaveBeenCalledWith('/api/v1/admin/org/sandbox-concurrency', {
      body: { sandbox_concurrency_limit: null },
    })
  })

  it('saves a new limit via the API', async () => {
    const wrapper = mountView()

    await nextTick()
    await nextTick()
    await nextTick()

    const input = wrapper.find('[data-testid="admin-sandbox-concurrency-limit"]')
    await input.setValue('12')
    await nextTick()

    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await nextTick()

    const { api } = await import('../lib/api/client')
    expect(api.PUT).toHaveBeenCalledWith('/api/v1/admin/org/sandbox-concurrency', {
      body: { sandbox_concurrency_limit: 12 },
    })
  })

  it('shows the updated message after a successful save', async () => {
    const wrapper = mountView()

    await nextTick()
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Sandbox concurrency limit updated.')
  })

  it('shows an error when the API call fails', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/org/sandbox-concurrency') {
        return Promise.resolve({ data: null, error: { detail: 'Failed to load sandbox concurrency' } })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { license: { tier: 'team' }, flags: [], would_activate: [] }, error: null })
      }
      return Promise.resolve({ data: null, error: null })
    })

    const wrapper = mountView()

    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Failed to load sandbox concurrency')
  })
})
