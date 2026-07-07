import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    PUT: vi.fn(),
    POST: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../lib/api/schema', () => ({}))

import SettingsEmailView from '../views/SettingsEmailView.vue'

function createWrapper() {
  return mount(SettingsEmailView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
        LoadingSpinner: { template: '<div class="animate-spin">Loading...</div>' },
        ErrorAlert: { template: '<div class="error-alert">{{ message?.detail || message }}</div>' },
      },
    },
  })
}

async function setupPlanStore(orgId: string) {
  const { usePlanStore } = await import('../stores/planStore')
  const planStore = usePlanStore()
  planStore.orgId = orgId
  return planStore
}

describe('SettingsEmailView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    await setupPlanStore('00000000-0000-0000-0000-000000000001')
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { smtp_host: '', smtp_port: 587, smtp_username: '', smtp_password: '********', email_from: '' }, error: undefined })

    const wrapper = createWrapper()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Email Settings')
  })

  it('loads and displays settings', async () => {
    await setupPlanStore('00000000-0000-0000-0000-000000000001')
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({
      data: {
        smtp_host: 'smtp.example.com',
        smtp_port: 587,
        smtp_username: 'user',
        smtp_password: '********',
        email_from: 'noreply at example.com',
      },
      error: undefined,
    })

    const wrapper = createWrapper()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Email Settings')
    expect(wrapper.text()).not.toContain('Loading...')
  })

  it('saves settings on save button click', async () => {
    await setupPlanStore('00000000-0000-0000-0000-000000000001')
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { smtp_host: '', smtp_port: 587, smtp_username: '', smtp_password: '********', email_from: '' }, error: undefined })
    ;(api.PUT as any).mockResolvedValue({ data: { smtp_host: 'smtp.new.com', smtp_port: 465, smtp_username: 'newuser', smtp_password: '********', email_from: 'new@example.com' }, error: undefined })

    const wrapper = createWrapper()
    await flushPromises()
    await nextTick()

    const saveBtn = wrapper.find('button')
    expect(saveBtn.exists()).toBe(true)
    await saveBtn.trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Email settings saved.')
  })
})
