import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const communityState = vi.hoisted(() => ({
  enabled: true,
  error: undefined as unknown,
  reject: false,
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/admin/org/community-objects') {
        if (communityState.reject) {
          return Promise.reject(new Error('network down'))
        }
        return Promise.resolve({
          data: { community_objects_enabled: communityState.enabled },
          error: communityState.error,
        })
      }
      if (url === '/api/v1/admin/billing/overview') {
        return Promise.resolve({
          data: {
            total_users: 5,
            total_teams: 2,
            total_pipelines: 12,
            plan_tier: 'community',
            plan_id: 'community',
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/org') {
        return Promise.resolve({
          data: {
            id: '00000000-0000-0000-0000-000000000001',
            name: 'Test Org',
            slug: 'test-org',
            created_at: '2025-01-15T00:00:00+00:00',
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/org/export') {
        return Promise.resolve({
          data: {
            exported_at: '2025-06-30T12:00:00+00:00',
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: 'Unknown route' })
    }),
    POST: vi.fn(),
    PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PATCH: vi.fn(),
    DELETE: vi.fn().mockResolvedValue({
      data: { message: 'Organisation has been permanently deleted.', deleted_organisation_id: '00000000-0000-0000-0000-000000000001', hard_deleted_runs: 0 },
      error: undefined,
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

const mockPush = vi.fn()
vi.mock('vue-router', async () => {
  const actual = await vi.importActual('vue-router')
  return {
    ...actual as any,
    useRouter: () => ({ push: mockPush }),
    useRoute: () => ({ path: '/admin/org' }),
  }
})

import AdminOrgSettingsView from '../views/AdminOrgSettingsView.vue'
import { usePlanStore } from '../stores/planStore'
import { api } from '../lib/api/client'

async function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.$patch({ features: { team_rbac: true }, currentTier: 'team' })
  const wrapper = mount(AdminOrgSettingsView, {
    global: { plugins: [pinia] },
  })
  for (let i = 0; i < 5; i++) {
    await nextTick()
  }
  return wrapper
}

describe('AdminOrgSettingsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPush.mockClear()
    communityState.enabled = true
    communityState.error = undefined
    communityState.reject = false
    vi.mocked(api.PUT).mockResolvedValue({ data: {}, error: undefined } as any)
  })

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Organisation Settings')
  })

  it('renders the organisation info section', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Organisation Info')
    expect(wrapper.text()).toContain('Test Org')
    expect(wrapper.text()).toContain('test-org')
    expect(wrapper.text()).toContain('5')
  })

  it('renders the data export section', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Data Export')
    expect(wrapper.text()).toContain('Export All Data')
  })

  it('renders the delete organisation section', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Delete Organisation')
    expect(wrapper.text()).toContain('Permanently delete')
  })

  it('shows org ID in the info section', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Org ID')
    expect(wrapper.text()).toContain('test-org')
  })

  it('displays the plan badge', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Community')
  })

  it('enables delete confirm button when correct org name is typed', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { team_rbac: true }, currentTier: 'team' })
    const wrapper = mount(AdminOrgSettingsView, {
      global: { plugins: [pinia], stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
      attachTo: document.body,
    })
    for (let i = 0; i < 10; i++) {
      await nextTick()
    }

    const deleteBtn = wrapper.findAll('button').filter(b => b.text().includes('Delete Organisation'))
    expect(deleteBtn.length).toBeGreaterThan(0)
    await deleteBtn[0].trigger('click')
    await nextTick()

    const input = document.querySelector('input[data-testid="org-delete-confirm-input"]') as HTMLInputElement
    expect(input).not.toBeNull()

    input.value = 'Wrong Name'
    input.dispatchEvent(new Event('input'))
    await nextTick()

    const confirmBtn = Array.from(document.querySelectorAll('button')).find(button => button.textContent?.includes('Permanently Delete')) as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)

    input.value = 'Test Org'
    input.dispatchEvent(new Event('input'))
    await nextTick()

    expect(confirmBtn.disabled).toBe(false)
    wrapper.unmount()
  })

  it('renders the community objects kill switch', async () => {
    const wrapper = await mountView()
    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-checked')).toBe('true')
    wrapper.unmount()
  })

  it('reflects a disabled community objects flag loaded from the API', async () => {
    communityState.enabled = false
    const wrapper = await mountView()
    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('false')
    wrapper.unmount()
  })

  it('fails open (stays enabled) when the community objects flag cannot be loaded', async () => {
    communityState.reject = true
    const wrapper = await mountView()
    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('true')
    wrapper.unmount()
  })

  it('toggles community objects off and persists the change', async () => {
    const wrapper = await mountView()
    const putMock = vi.mocked(api.PUT)
    putMock.mockResolvedValue({ data: {}, error: undefined } as any)

    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    await toggle.trigger('click')
    await vi.waitFor(() => {
      expect(toggle.attributes('aria-checked')).toBe('false')
    })

    expect(putMock).toHaveBeenCalledWith(
      '/api/v1/admin/org/community-objects',
      expect.objectContaining({ body: { community_objects_enabled: false } }),
    )
    wrapper.unmount()
  })

  it('shows an error when the community objects toggle request fails', async () => {
    const wrapper = await mountView()
    vi.mocked(api.PUT).mockResolvedValueOnce({ data: null, error: 'Server exploded' } as any)

    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    await toggle.trigger('click')
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Server exploded')
    })
    expect(toggle.attributes('aria-checked')).toBe('true')
    wrapper.unmount()
  })

  it('shows an error when the community objects toggle request throws', async () => {
    const wrapper = await mountView()
    vi.mocked(api.PUT).mockRejectedValueOnce(new Error('network down'))

    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    await toggle.trigger('click')
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('network down')
    })
    wrapper.unmount()
  })
})
