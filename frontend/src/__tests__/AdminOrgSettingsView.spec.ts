import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
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
      if (url === '/api/v1/admin/org/export') {
        return Promise.resolve({
          data: {
            organisation: {
              id: '00000000-0000-0000-0000-000000000001',
              name: 'Test Org',
              slug: 'test-org',
              created_at: '2025-01-15T00:00:00+00:00',
            },
            exported_at: '2025-06-30T12:00:00+00:00',
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: 'Unknown route' })
    }),
    POST: vi.fn(),
    PUT: vi.fn(),
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

async function mountView() {
  const pinia = createPinia()
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
    expect(wrapper.text()).toContain('00000000')
  })

  it('displays the plan badge', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Community')
  })

  it('enables delete confirm button when correct org name is typed', async () => {
    const pinia = createPinia()
    const wrapper = mount(AdminOrgSettingsView, {
      global: { plugins: [pinia] },
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

    const confirmBtn = document.querySelector('button[data-testid="org-delete-confirm-button"]') as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)

    input.value = 'Test Org'
    input.dispatchEvent(new Event('input'))
    await nextTick()

    expect(confirmBtn.disabled).toBe(false)
    wrapper.unmount()
  })
})
