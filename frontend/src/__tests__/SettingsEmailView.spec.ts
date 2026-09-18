import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const { mockPlanStore, mockApi } = vi.hoisted(() => {
  const ORG_ID = '00000000-0000-0000-0000-000000000001'
  return {
    mockPlanStore: {
      orgId: ORG_ID as string | null,
      fetchPlan: vi.fn().mockResolvedValue(undefined),
      featureEnabled: vi.fn().mockReturnValue(true),
      isAtMinimumTier: vi.fn().mockReturnValue(true),
    },
    mockApi: {
      GET: vi.fn(),
      PUT: vi.fn(),
      POST: vi.fn(),
      getAccessToken: vi.fn().mockReturnValue('mock-token'),
    },
  }
})

vi.mock('../stores/planStore', () => ({
  usePlanStore: () => mockPlanStore,
}))

vi.mock('../lib/api/client', () => ({
  api: mockApi,
}))

import SettingsEmailView from '../views/SettingsEmailView.vue'

const ORG_ID = '00000000-0000-0000-0000-000000000001'

function okLoad() {
  mockApi.GET.mockResolvedValue({
    data: {
      smtp_host: 'smtp.example.com',
      smtp_port: 587,
      smtp_username: 'user',
      email_from: 'a@b.com',
      smtp_timeout: 30,
    },
    error: undefined,
  })
}

async function mountView(orgId: string | null = ORG_ID) {
  mockPlanStore.orgId = orgId
  mockPlanStore.fetchPlan.mockResolvedValue(undefined)
  mockPlanStore.featureEnabled.mockReturnValue(true)
  okLoad()
  const wrapper = mount(SettingsEmailView)
  await flushPromises()
  await nextTick()
  await nextTick()
  return wrapper
}

describe('SettingsEmailView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPlanStore.orgId = ORG_ID
    mockPlanStore.featureEnabled.mockReturnValue(true)
    okLoad()
  })

  it('renders the email settings form', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="settings-email-smtp-host"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-email-save"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-email-test"]').exists()).toBe(true)
  })

  it('shows an error when the organisation id is not available at load', async () => {
    const wrapper = await mountView(null)
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Organisation ID not available')
  })

  it('saves settings successfully and sends clear_password: false', async () => {
    mockApi.PUT.mockResolvedValue({ data: { ok: true }, error: undefined })
    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-email-save"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(mockApi.PUT).toHaveBeenCalledTimes(1)
    const callBody = mockApi.PUT.mock.calls[0][1].body
    expect(callBody.clear_password).toBe(false)
    expect(wrapper.text()).toContain('Email settings saved.')
  })

  it('shows a save failure message when the API returns an error', async () => {
    mockApi.PUT.mockResolvedValue({ data: undefined, error: { detail: 'boom' } })
    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-email-save"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Save failed: boom')
  })

  it('shows a save failure message when the request throws', async () => {
    mockApi.PUT.mockRejectedValue(new Error('network'))
    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-email-save"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Save failed: network')
  })

  it('shows a failure when organisation cannot be determined on save', async () => {
    const wrapper = await mountView()
    mockPlanStore.orgId = null
    await wrapper.find('[data-testid="settings-email-save"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Could not determine organisation')
  })

  it('reports a successful test email', async () => {
    mockApi.POST.mockResolvedValue({ data: { ok: true, message: 'sent' }, error: undefined })
    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-email-test"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('sent')
  })

  it('reports a failed test email via the result body', async () => {
    mockApi.POST.mockResolvedValue({ data: { ok: false, message: 'rejected' }, error: undefined })
    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-email-test"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('rejected')
  })

  it('reports a default failure when no result is returned', async () => {
    mockApi.POST.mockResolvedValue({ data: undefined, error: undefined })
    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-email-test"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Test failed.')
  })

  it('reports a failure when the test request throws', async () => {
    mockApi.POST.mockRejectedValue(new Error('down'))
    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-email-test"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Test failed: down')
  })

  it('returns early when organisation id is missing on test', async () => {
    const wrapper = await mountView()
    mockPlanStore.orgId = null
    mockApi.POST.mockResolvedValue({ data: { ok: true }, error: undefined })
    await wrapper.find('[data-testid="settings-email-test"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(mockApi.POST).not.toHaveBeenCalled()
  })
})
