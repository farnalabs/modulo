import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() { await vueNextTick(); await flushPromises() }

const mockLicenseFree = {
  has_license: false,
  tier: 'community',
  features: [],
  expires_at: null,
  org_id: null,
}

const mockLicenseTeam = {
  has_license: true,
  tier: 'team',
  features: ['parallel_branches', 'eval_system'],
  expires_at: '2026-12-31T23:59:59Z',
  org_id: '11111111-2222-3333-4444-555555555555',
}

const mockFlagsFree = {
  license: { tier: 'community', has_license_key: false, is_valid: true },
  flags: [
    { name: 'parallel_branches', description: 'Run parallel branches', tier: 'team', currently_active: false, depends_on: null },
    { name: 'hitl_gates', description: 'Human-in-the-loop gates', tier: 'community', currently_active: true, depends_on: null },
  ],
  would_activate: [
    { name: 'parallel_branches', description: 'Run parallel branches', tier: 'team', currently_active: false, depends_on: null },
  ],
}

const mockFlagsTeam = {
  license: { tier: 'team', has_license_key: true, is_valid: true },
  flags: [
    { name: 'parallel_branches', description: 'Run parallel branches', tier: 'team', currently_active: true, depends_on: null },
    { name: 'hitl_gates', description: 'Human-in-the-loop gates', tier: 'community', currently_active: true, depends_on: null },
  ],
  would_activate: [],
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn().mockResolvedValue({ data: { tier: 'team', expires_at: '2027-01-01' }, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
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

// Object form of the stubs (name -> true) so we can also override FormDialog.
const dialogStubObj = Object.fromEntries(dialogStubs.map((name) => [name, true]))

function mockApiResponses(getMock: any, licenseData: any, flagsData: any) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/v1/admin/license') {
      return Promise.resolve({ data: licenseData, error: undefined })
    }
    if (path === '/api/v1/admin/feature-flags') {
      return Promise.resolve({ data: flagsData, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
}

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

  it('renders license key textarea and action buttons', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.find('textarea').exists()).toBe(true)
    expect(wrapper.text()).toContain('Verify Key')
    expect(wrapper.text()).toContain('Apply Key')
  })

  it('verifyKey shows a valid license message with an expiry date', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.POST as any).mockResolvedValue({ data: { tier: 'team', expires_at: '2027-01-01' }, error: undefined })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('textarea').setValue('some-key')
    await wrapper.find('[data-testid="license-verify-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    expect(wrapper.text()).toContain('Valid license key')
  })

  it('verifyKey shows a never-expiry fallback when expires_at is absent', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.POST as any).mockResolvedValue({ data: { tier: 'team' }, error: undefined })

    const wrapper = mount(SettingsLicenseView, {
      global: { plugins: [createPinia()], stubs: { ...dialogStubObj, FormDialog: formDialogStub } },
    })
    await nextTick(); await nextTick(); await nextTick()
    await wrapper.find('textarea').setValue('some-key')
    await wrapper.find('[data-testid="license-verify-btn"]').trigger('click')
    await nextTick(); await flushPromises(); await nextTick()
    expect(wrapper.text()).toContain('never')
  })

  it('applyKey surfaces an error when the API returns an error', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockLicenseTeam, mockFlagsTeam)
    ;(api.POST as any).mockResolvedValue({ data: null, error: 'boom' })

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
    ;(api.POST as any).mockRejectedValue(new Error('network-down'))

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
})
