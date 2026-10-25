import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { computed, nextTick as vueNextTick, ref } from 'vue'

async function nextTick() { await vueNextTick(); await flushPromises() }

const mockLicenseFree = {
  has_license: false,
  tier: 'community',
  features: [],
  expires_at: null,
  org_id: null,
  renewal_mode: 'manual',
  licence_state: 'none',
}

const mockLicenseTeam = {
  has_license: true,
  tier: 'team',
  features: ['parallel_branches', 'eval_system'],
  expires_at: '2026-12-31T23:59:59Z',
  org_id: '11111111-2222-3333-4444-555555555555',
  renewal_mode: 'manual',
  licence_state: 'active',
}

const mockFlagsFree = {
  license: { tier: 'community', has_license_key: false, is_valid: true },
  flags: [
    { name: 'parallel_branches', description: 'Run parallel branches', tier: 'team', currently_active: false, depends_on: null },
    { name: 'hitl_reviews', description: 'Human-in-the-loop gates', tier: 'community', currently_active: true, depends_on: null },
  ],
  would_activate: [
    { name: 'parallel_branches', description: 'Run parallel branches', tier: 'team', currently_active: false, depends_on: null },
  ],
}

const mockFlagsTeam = {
  license: { tier: 'team', has_license_key: true, is_valid: true },
  flags: [
    { name: 'parallel_branches', description: 'Run parallel branches', tier: 'team', currently_active: true, depends_on: null },
    { name: 'hitl_reviews', description: 'Human-in-the-loop gates', tier: 'community', currently_active: true, depends_on: null },
  ],
  would_activate: [],
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn().mockResolvedValue({ data: { tier: 'team', expires_at: '2027-01-01' }, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: { tier: 'team' }, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ error: undefined }),
  },
  // A decodable token: the org-scoped licence controls need `org_id` from the
  // claims (FAR-2104), and the role/system flag come from the same payload.
  getAccessToken: vi.fn().mockReturnValue(
    `x.${btoa(JSON.stringify({ sub: 'admin@modulo.run', org_id: 'org-1', org_role: 'admin' }))}.y`,
  ),
}))

import SettingsLicenseView from '../views/SettingsLicenseView.vue'

const dialogStubs = ['Dialog', 'DialogContent', 'DialogDescription', 'DialogFooter', 'DialogHeader', 'DialogTitle']

// Stub FormDialog so its @confirm handler (applyKey / removeLicense) can be
// triggered without rendering the full primevue Dialog tree.
const formDialogStub = {
  props: ['open', 'title', 'description', 'confirmText', 'loading'],
  emits: ['confirm'],
  template: `<button v-if="open" class="formdialog-confirm" @click="$emit('confirm')">{{ confirmText }}</button>`,
}

// Renders the renewal notice's dialog body + footer so the grace actions are
// reachable (a bare `true` stub would swallow the slot content).
const primeDialogStub = {
  name: 'Dialog',
  props: ['visible'],
  template: '<div v-if="visible"><slot /><slot name="footer" /></div>',
}

// Object form of the stubs (name -> true) so we can also override FormDialog.
const dialogStubObj = Object.fromEntries(dialogStubs.map((name) => [name, true]))

// The page's licence audience is derived from the caller's server-resolved
// permission list (`GET /api/v1/auth/permissions`) — the same registry the
// licence endpoints enforce — so every mount answers that read. Default: an
// org admin (org.license.manage), which is the page's intended audience.
function mockApiResponses(
  getMock: any,
  licenseData: any,
  flagsData: any,
  permissions: string[] = ['org.license.manage'],
) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/v1/admin/license') {
      return Promise.resolve({ data: licenseData, error: undefined })
    }
    if (path === '/api/v1/auth/permissions') {
      return Promise.resolve({ data: { permissions }, error: undefined })
    }
    if (path === '/api/v1/admin/feature-flags') {
      return Promise.resolve({ data: flagsData, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
}

const SYSTEM_PERMISSIONS = ['org.license.manage', 'system.config.manage']

describe('SettingsLicenseView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseFree, mockFlagsFree)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
  })

  it('shows loading spinner initially', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockReturnValue(new Promise(() => {}))

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })

  it('shows error alert on API failure', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockImplementation(() => Promise.resolve({ data: null, error: 'License API error' }))

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('License API error')
  })

  it('displays Free Tier content', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseFree, mockFlagsFree)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Community')
    expect(wrapper.text()).toContain('Get a Team License')
  })

  it('displays Team tier content', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Team')
    expect(wrapper.text()).toContain('#11111111')
    expect(wrapper.text()).toContain('December')
    expect(wrapper.text()).toContain('Team license key active')
  })

  it('does not render per-feature checklist (simplified UI)', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).not.toContain('parallel_branches')
    expect(wrapper.text()).not.toContain('features active')
    expect(wrapper.text()).not.toContain('would activate with Team')
  })

  it('an org admin gets the organisation key controls and NOT the system controls', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.find('[data-testid="licence-org-controls"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="licence-system-controls"]').exists()).toBe(false)
    // FAR-2110: the instance-wide Remove control is the system admin's alone.
    expect(wrapper.find('[data-testid="license-remove-btn-system"]').exists()).toBe(false)
    expect(wrapper.find('textarea').exists()).toBe(true)
    expect(wrapper.text()).toContain('Apply Key')
    expect(wrapper.text()).toContain('Organisation License')
    expect(wrapper.text()).not.toContain('Verify Key')
  })

  it('a system admin gets the system-level controls as well as the org ones', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.find('[data-testid="licence-system-controls"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="licence-org-controls"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="license-remove-btn-system"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Verify Key')
    expect(wrapper.text()).toContain('Install System License')
  })

  it('a principal without org.license.manage gets neither control section', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, [])

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.find('[data-testid="licence-org-controls"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-system-controls"]').exists()).toBe(false)
  })

  it('verifyKey shows a valid license message with an expiry date', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)
    ;(api.POST as any).mockResolvedValue({ data: { tier: 'team', expires_at: '2027-01-01' }, error: undefined })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('[data-testid="license-key-input-system"]').setValue('some-key')
    await wrapper.find('[data-testid="license-verify-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    expect(wrapper.text()).toContain('Valid license key')
  })

  it('verifyKey shows a never-expiry fallback when expires_at is absent', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)
    ;(api.POST as any).mockResolvedValue({ data: { tier: 'team' }, error: undefined })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('[data-testid="license-key-input-system"]').setValue('some-key')
    await wrapper.find('[data-testid="license-verify-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    expect(wrapper.text()).toContain('never')
  })

  it('verifyKey surfaces the server error detail instead of [object Object]', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)
    ;(api.POST as any).mockResolvedValue({
      data: null,
      error: {
        type: 'urn:problem:modulo:bad_request',
        title: 'Bad Request',
        status: 400,
        detail: 'Licence key is not signed by the configured trust anchor',
      },
    })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('[data-testid="license-key-input-system"]').setValue('some-key')
    await wrapper.find('[data-testid="license-verify-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    expect(wrapper.text()).toContain('Licence key is not signed by the configured trust anchor')
    expect(wrapper.text()).not.toContain('[object Object]')
  })

  it('an org admin installs the key through the org-scoped endpoint', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.PUT as any).mockResolvedValue({ data: { tier: 'team' }, error: undefined })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('textarea').setValue('org-key')
    await wrapper.find('[data-testid="license-apply-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()

    expect(api.PUT).toHaveBeenCalledWith('/api/v1/admin/orgs/org-1/license', {
      body: { license_key: 'org-key' },
    })
    expect(api.POST).not.toHaveBeenCalled()
  })

  it('a system admin installs the instance-wide key through the system endpoint', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)
    ;(api.POST as any).mockResolvedValue({ data: { tier: 'team' }, error: undefined })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('[data-testid="license-key-input-system"]').setValue('system-key')
    await wrapper.find('[data-testid="license-apply-btn-system"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/admin/license', {
      body: { license_key: 'system-key' },
    })
  })

  it('a system admin removes the INSTANCE-WIDE key through the system endpoint', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)
    ;(api.DELETE as any).mockResolvedValue({ error: undefined })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('[data-testid="license-remove-btn-system"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()

    expect(api.DELETE).toHaveBeenCalledWith('/api/v1/admin/license')
    // The org-scoped route is NOT touched by the system control.
    expect(api.DELETE).not.toHaveBeenCalledWith('/api/v1/admin/orgs/org-1/license')
  })

  it('a system admin sees the system Remove control only when a licence is installed', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseFree, mockFlagsFree, SYSTEM_PERMISSIONS)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick(); await nextTick(); await nextTick()
    expect(wrapper.find('[data-testid="licence-system-controls"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="license-remove-btn-system"]').exists()).toBe(false)
  })

  it('applyKey surfaces an error when the API returns an error', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.PUT as any).mockResolvedValue({ data: null, error: 'boom' })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('textarea').setValue('some-key')
    await wrapper.find('[data-testid="license-apply-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    expect(wrapper.text()).toContain('Failed to apply')
  })

  it('applyKey surfaces an error when the API call throws', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.PUT as any).mockRejectedValue(new Error('network-down'))

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('textarea').setValue('some-key')
    await wrapper.find('[data-testid="license-apply-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    expect(wrapper.text()).toContain('Failed to apply')
  })

  it('removeLicense surfaces an error when the API returns an error', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.DELETE as any).mockResolvedValue({ error: 'nope' })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    const removeBtn = wrapper.findAll('button').find((b) => b.text() === 'Remove License')
    expect(removeBtn).toBeTruthy()
    await removeBtn!.trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    expect(wrapper.text()).toContain('Failed to remove')
  })

  it('an org admin removes the ORGANISATION key through the org-scoped endpoint', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.DELETE as any).mockResolvedValue({ error: undefined })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    const removeBtn = wrapper.findAll('button').find((b) => b.text() === 'Remove License')
    await removeBtn!.trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()

    expect(api.DELETE).toHaveBeenCalledWith('/api/v1/admin/orgs/org-1/license')
  })

  it('removeLicense surfaces an error when the API call throws', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.DELETE as any).mockRejectedValue(new Error('network-down'))

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    const removeBtn = wrapper.findAll('button').find((b) => b.text() === 'Remove License')
    expect(removeBtn).toBeTruthy()
    await removeBtn!.trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    expect(wrapper.text()).toContain('Failed to remove')
  })

  // ── Guards on the two licence scopes (FAR-2104) ─────────────────────

  it('verifyKey does nothing when the system key field is empty', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()

    // The button is disabled while the field is empty; the guard is the last
    // line of defence for a programmatic call.
    await (wrapper.vm as any).verifyKey()
    expect(api.POST).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Valid license key')
  })

  it('applyKey does nothing when the org key field is empty', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()

    await (wrapper.vm as any).applyKey()
    expect(api.PUT).not.toHaveBeenCalled()
  })

  it('applySystemKey does nothing when the system key field is empty', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()

    await (wrapper.vm as any).applySystemKey()
    expect(api.POST).not.toHaveBeenCalled()
  })

  it('applySystemKey surfaces an error when the API returns an error', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)
    ;(api.POST as any).mockResolvedValue({ data: null, error: 'boom' })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('[data-testid="license-key-input-system"]').setValue('system-key')
    await wrapper.find('[data-testid="license-apply-btn-system"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/admin/license', {
      body: { license_key: 'system-key' },
    })
    expect(wrapper.text()).toContain('Failed to apply')
  })

  it('applySystemKey surfaces an error when the API call throws', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam, SYSTEM_PERMISSIONS)
    ;(api.POST as any).mockRejectedValue(new Error('network-down'))

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('[data-testid="license-key-input-system"]').setValue('system-key')
    await wrapper.find('[data-testid="license-apply-btn-system"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()

    expect(wrapper.text()).toContain('Failed to apply')
  })

  it('removeLicense does nothing when the caller has no org id', async () => {
    const { api, getAccessToken } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    const withoutOrg = `x.${btoa(JSON.stringify({ sub: 'admin@modulo.run', org_role: 'admin' }))}.y`
    vi.mocked(getAccessToken).mockReturnValue(withoutOrg)

    try {
      const wrapper = mount(SettingsLicenseView, {
        global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
      })
      await nextTick(); await nextTick(); await nextTick()

      // Without an org there is no org-scoped endpoint to call.
      await (wrapper.vm as any).removeLicense()
      expect(api.DELETE).not.toHaveBeenCalled()
    } finally {
      vi.mocked(getAccessToken).mockReturnValue(
        `x.${btoa(JSON.stringify({ sub: 'admin@modulo.run', org_id: 'org-1', org_role: 'admin' }))}.y`,
      )
    }
  })

  // ── FAR-2075: shell-owned renewal surface ───────────────────────────

  it('does not render the renewal surface itself — the authenticated shell owns it', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(
      api.GET,
      { ...mockLicenseTeam, licence_state: 'renewal_due', renewal_mode: 'manual', days_until_expiry: 9 },
      mockFlagsTeam,
    )

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, Dialog: primeDialogStub } },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    // Mounting it here as well would show two banners (and, in grace, two
    // dialogs) on this one page.
    expect(wrapper.find('[data-testid="licence-renewal-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-grace-dialog"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-lapsed-banner"]').exists()).toBe(false)
  })

  it('reloads its own licence status after a key is applied when mounted without the shell', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.PUT as any).mockResolvedValue({ data: { tier: 'team', expires_at: '2027-01-01' }, error: undefined })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()

    const licenceGets = () =>
      (api.GET as any).mock.calls.filter(([path]: [string]) => path === '/api/v1/admin/license')
    expect(licenceGets()).toHaveLength(1)

    await wrapper.find('textarea').setValue('some-key')
    await wrapper.find('[data-testid="license-apply-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()

    expect(licenceGets()).toHaveLength(2)
  })

  it('reuses the shell-provided licence status instead of fetching it again', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)

    const status = ref({ ...mockLicenseTeam, licence_state: 'renewal_due', days_until_expiry: 9 })
    const source = {
      data: status,
      loading: computed(() => false),
      error: computed(() => null),
      load: vi.fn(async () => {}),
    }

    const wrapper = mount(SettingsLicenseView, {
      global: {
        plugins: [createPinia()],
        provide: { 'modulo:licence-status': source },
        stubs: { ...dialogStubObj, Dialog: primeDialogStub },
      },
    })
    await nextTick()
    await nextTick()

    // Rendered from the provided status…
    expect(wrapper.text()).toContain('Team')
    expect(wrapper.text()).toContain('#11111111')
    // …and no second GET against the endpoint the shell already read.
    const licenceGets = (api.GET as any).mock.calls.filter(
      ([path]: [string]) => path === '/api/v1/admin/license',
    )
    expect(licenceGets).toHaveLength(0)
  })

  it('refreshes the shared status after a licence key is applied', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.PUT as any).mockResolvedValue({ data: { tier: 'team', expires_at: '2027-01-01' }, error: undefined })

    const source = {
      data: ref({ ...mockLicenseTeam, licence_state: 'lapsed', has_license: false, tier: 'community' }),
      loading: computed(() => false),
      error: computed(() => null),
      load: vi.fn(async () => {}),
    }

    const wrapper = mount(SettingsLicenseView, {
      global: {
        plugins: [createPinia()],
        provide: { 'modulo:licence-status': source },
        stubs: { ...dialogStubObj, FormDialog: formDialogStub },
      },
    })
    await nextTick()
    await wrapper.find('textarea').setValue('some-key')
    await wrapper.find('[data-testid="license-apply-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    await wrapper.find('.formdialog-confirm').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()

    // The shell's banner clears only if the shared query is refreshed.
    expect(source.load).toHaveBeenCalledTimes(1)
  })
})
