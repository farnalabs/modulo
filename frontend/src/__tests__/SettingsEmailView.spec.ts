import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { usePlanStore } from '../stores/planStore'

const { mockApi, ORG_IDS } = vi.hoisted(() => {
  // FAR-2368: two DISTINCT orgs. The caller is authenticated against
  // `authenticated` (the JWT `org_id` claim); the deployment licence names
  // `licence` (`license.org_id` in GET /api/v1/plan). Only the former may ever
  // address an org-scoped endpoint.
  const authenticated = '00000000-0000-0000-0000-0000000000aa'
  const licence = '00000000-0000-0000-0000-0000000000bb'
  return {
    ORG_IDS: { authenticated, licence },
    mockApi: {
      GET: vi.fn(),
      PUT: vi.fn(),
      POST: vi.fn(),
      getAccessToken: vi.fn(),
    },
  }
})

vi.mock('../lib/api/client', () => ({
  api: mockApi,
  getAccessToken: mockApi.getAccessToken,
}))

import SettingsEmailView from '../views/SettingsEmailView.vue'

/** The JWT the backend issues: its `org_id` claim is the AUTHENTICATED org. */
function authToken(orgId: string | null): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))
  const claims: Record<string, unknown> = { sub: 'admin@modulo.run', org_role: 'admin' }
  if (orgId) claims.org_id = orgId
  const body = btoa(JSON.stringify(claims))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '')
  return `${header}.${body}.signature`
}

function okLoad(emailError = false) {
  mockApi.GET.mockImplementation((path: string) => {
    if (path === '/api/v1/plan') {
      return Promise.resolve({
        data: {
          license: { tier: 'team', org_id: ORG_IDS.licence },
          flags: [{ name: 'email_config', currently_active: true }],
        },
        error: undefined,
      })
    }
    if (path === '/api/v1/admin/tiers') {
      return Promise.resolve({
        data: { tiers: [{ tier_id: 'team', label: 'Team', rank: 1 }] },
        error: undefined,
      })
    }
    if (emailError) return Promise.reject(new Error('settings unavailable'))
    return Promise.resolve({
      data: {
        smtp_host: 'smtp.example.com',
        smtp_port: 587,
        smtp_username: 'user',
        email_from: 'a@b.com',
        smtp_timeout: 30,
      },
      error: undefined,
    })
  })
}

async function preparePlanStore() {
  setActivePinia(createPinia())
  const planStore = usePlanStore()
  await planStore.fetchPlan()
  mockApi.GET.mockClear()
  return planStore
}

async function mountView(authenticatedOrg: string | null = ORG_IDS.authenticated) {
  mockApi.getAccessToken.mockReturnValue(authToken(authenticatedOrg))
  await preparePlanStore()
  const wrapper = mount(SettingsEmailView)
  await flushPromises()
  await nextTick()
  await nextTick()
  return wrapper
}

describe('SettingsEmailView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockApi.getAccessToken.mockReturnValue(authToken(ORG_IDS.authenticated))
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

  // FAR-2368 regression: the load/save/test endpoints all authorise the caller
  // against the org in the PATH. The target org must be the authenticated org
  // from the JWT, never the licence's org — on staging the licence named a
  // different org, so the view addressed another org's settings and the backend
  // correctly answered 403 "requires 'operator' role in target organisation".
  it('targets the authenticated org on load, save and test even when the licence names another org', async () => {
    mockApi.PUT.mockResolvedValue({ data: { ok: true }, error: undefined })
    mockApi.POST.mockResolvedValue({ data: { ok: true, message: 'sent' }, error: undefined })

    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-email-save"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="settings-email-test"]').trigger('click')
    await flushPromises()

    // The plan payload's licence org is a real, different org in this test.
    expect(usePlanStore().orgId).toBe(ORG_IDS.licence)

    // READ: email-settings is fetched for the authenticated org.
    expect(mockApi.GET).toHaveBeenCalledWith('/api/v1/admin/org/{org_id}/email-settings', {
      params: { path: { org_id: ORG_IDS.authenticated } },
    })

    // UPDATE: same target org, and the real EmailSettingsUpdate payload shape
    // (backend `admin_email.EmailSettingsUpdate`).
    expect(mockApi.PUT).toHaveBeenCalledWith('/api/v1/admin/org/{org_id}/email-settings', {
      params: { path: { org_id: ORG_IDS.authenticated } },
      body: {
        smtp_host: 'smtp.example.com',
        smtp_port: 587,
        smtp_username: 'user',
        smtp_password: '',
        email_from: 'a@b.com',
        smtp_timeout: 30,
        clear_password: false,
      },
    })

    // TEST-SEND: same target org again.
    expect(mockApi.POST).toHaveBeenCalledWith('/api/v1/admin/org/{org_id}/email-settings/test', {
      params: { path: { org_id: ORG_IDS.authenticated } },
      body: { to: 'test@example.com' },
    })

    // The licence's org is never addressed on any of the three paths.
    expect(mockApi.GET).not.toHaveBeenCalledWith(expect.anything(), {
      params: { path: { org_id: ORG_IDS.licence } },
    })
    expect(mockApi.PUT).not.toHaveBeenCalledWith(expect.anything(), expect.objectContaining({
      params: { path: { org_id: ORG_IDS.licence } },
    }))
    expect(mockApi.POST).not.toHaveBeenCalledWith(expect.anything(), expect.objectContaining({
      params: { path: { org_id: ORG_IDS.licence } },
    }))
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
    // The form renders from a valid session, then the token loses its org
    // claim (e.g. an opaque/expired token) before the save is attempted.
    mockApi.getAccessToken.mockReturnValue(authToken(null))
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
    mockApi.getAccessToken.mockReturnValue(authToken(null))
    mockApi.POST.mockResolvedValue({ data: { ok: true }, error: undefined })
    await wrapper.find('[data-testid="settings-email-test"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(mockApi.POST).not.toHaveBeenCalled()
  })

  it('clears a success message when its timer expires and cleans up on unmount', async () => {
    vi.useFakeTimers()
    mockApi.POST.mockResolvedValue({ data: { ok: true, message: 'sent' }, error: undefined })
    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-email-test"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('sent')
    vi.advanceTimersByTime(5000)
    await nextTick()
    expect(wrapper.text()).not.toContain('sent')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('shows a load failure and allows retry', async () => {
    mockApi.getAccessToken.mockReturnValue(authToken(ORG_IDS.authenticated))
    await preparePlanStore()
    okLoad(true)
    const wrapper = mount(SettingsEmailView)
    await flushPromises()
    expect(wrapper.text()).toContain('settings unavailable')
    mockApi.GET.mockResolvedValueOnce({ data: { smtp_host: 'smtp.retry.test' }, error: undefined })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="settings-email-smtp-host"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
