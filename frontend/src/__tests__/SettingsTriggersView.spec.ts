import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { putMock } = vi.hoisted(() => ({ putMock: vi.fn() }))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ error: undefined }),
  },
  getAccessToken: vi.fn(),
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ put: putMock, get: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }),
}))

vi.mock('../lib/api/schema', () => ({}))

import SettingsTriggersView from '../views/SettingsTriggersView.vue'
import { api, getAccessToken } from '../lib/api/client'

const dialogStubs = ['Dialog', 'DialogContent', 'DialogDescription', 'DialogFooter', 'DialogHeader', 'DialogTitle']
const featureGateStub = { template: '<div><slot /></div>' }

function fakeJwt(orgRole: string): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))
  const payload = btoa(
    JSON.stringify({
      sub: 'test@example.com',
      org_id: '00000000-0000-0000-0000-000000000001',
      org_role: orgRole,
    }),
  )
  return `${header}.${payload}.signature`
}

function mountView(token: string, listData: Record<string, unknown>) {
  ;(api.GET as any).mockResolvedValue({ data: listData, error: undefined })
  ;(getAccessToken as any).mockReturnValue(token)
  return mount(SettingsTriggersView, {
    global: { stubs: { ...dialogStubs.reduce((a, k) => ({ ...a, [k]: true }), {}), FeatureGate: featureGateStub } },
  })
}

async function flush() {
  await nextTick()
  await nextTick()
  await nextTick()
}

const baseListData = { items: [], total: 0, page: 1, page_size: 100 }

describe('SettingsTriggersView — org-wide trigger pause', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    putMock.mockResolvedValue({ paused: true, paused_at: null })
  })

  it('admin: renders the pause toggle, PUT fires only on toggle click, UI reflects response', async () => {
    const wrapper = mountView(fakeJwt('admin'), { ...baseListData, triggers_paused: false, paused_at: null })
    await flush()

    // Toggle present, banner hidden while not paused.
    expect(wrapper.find('[data-testid="settings-triggers-pause-all"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-triggers-paused-banner"]').exists()).toBe(false)
    expect(putMock).not.toHaveBeenCalled()

    // Clicking the toggle PUTs the new value (not paused -> paused: true).
    await wrapper.find('[data-testid="settings-triggers-pause-all"]').trigger('click')
    await flush()
    expect(putMock).toHaveBeenCalledTimes(1)
    const [path, body] = putMock.mock.calls[0]
    expect(path).toContain('/api/v1/admin/orgs/')
    expect(path).toContain('/triggers/pause')
    expect(body).toEqual({ paused: true })

    // UI reflects the response (banner now shown).
    expect(wrapper.find('[data-testid="settings-triggers-paused-banner"]').exists()).toBe(true)
  })

  it('A11Y: banner announces status (role=status + aria-live) and toggle exposes aria-pressed', async () => {
    const wrapper = mountView(fakeJwt('admin'), { ...baseListData, triggers_paused: true, paused_at: '2026-08-04T00:00:00Z' })
    await flush()

    const banner = wrapper.find('[data-testid="settings-triggers-paused-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('role')).toBe('status')
    expect(banner.attributes('aria-live')).toBe('polite')

    const toggle = wrapper.find('[data-testid="settings-triggers-pause-all"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-pressed')).toBe('true')
  })

  it('STATE-1: failed toggle surfaces an inline pause error without clobbering the loaded list', async () => {
    putMock.mockRejectedValue(new Error('network down'))
    const wrapper = mountView(fakeJwt('admin'), {
      ...baseListData,
      items: [{ id: 't1', pipeline_id: 'p1', trigger_type: 'webhook', active: true, config_json: {} }],
      triggers_paused: false,
      paused_at: null,
    })
    await flush()

    expect(wrapper.find('[data-testid="settings-triggers-pause-error"]').exists()).toBe(false)

    await wrapper.find('[data-testid="settings-triggers-pause-all"]').trigger('click')
    await flush()

    const inlineError = wrapper.find('[data-testid="settings-triggers-pause-error"]')
    expect(inlineError.exists()).toBe(true)
    expect(inlineError.text()).toContain('Failed to update trigger pause state')
    expect(inlineError.attributes('role')).toBe('alert')

    expect(wrapper.find('[data-testid="settings-triggers-pause-all"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-triggers-paused-banner"]').exists()).toBe(false)
  })

  it('admin: banner shows when already paused and the button resumes (PUT paused: false)', async () => {
    const wrapper = mountView(fakeJwt('admin'), { ...baseListData, triggers_paused: true, paused_at: '2026-08-04T00:00:00Z' })
    await flush()

    expect(wrapper.find('[data-testid="settings-triggers-paused-banner"]').exists()).toBe(true)
    expect(putMock).not.toHaveBeenCalled()

    await wrapper.find('[data-testid="settings-triggers-pause-all"]').trigger('click')
    await flush()
    const [path, body] = putMock.mock.calls[0]
    expect(path).toContain('/triggers/pause')
    expect(body).toEqual({ paused: false })
  })

  it('non-admin: banner renders but toggle is absent and no PUT fires on mount', async () => {
    const wrapper = mountView(fakeJwt('viewer'), { ...baseListData, triggers_paused: true, paused_at: null })
    await flush()

    expect(wrapper.find('[data-testid="settings-triggers-paused-banner"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-triggers-pause-all"]').exists()).toBe(false)
    expect(putMock).not.toHaveBeenCalled()
  })

  it('banner shows when the list GET returns triggers_paused: true and hides when false', async () => {
    const paused = mountView(fakeJwt('admin'), { ...baseListData, triggers_paused: true, paused_at: null })
    await flush()
    expect(paused.find('[data-testid="settings-triggers-paused-banner"]').exists()).toBe(true)

    const resumed = mountView(fakeJwt('admin'), { ...baseListData, triggers_paused: false, paused_at: null })
    await flush()
    expect(resumed.find('[data-testid="settings-triggers-paused-banner"]').exists()).toBe(false)
  })
})

describe('SettingsTriggersView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mountView(fakeJwt('admin'), baseListData)
    await flush()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Triggers')
  })

  it('shows loading spinner initially', async () => {
    ;(api.GET as any).mockReturnValue(new Promise(() => {}))

    const wrapper = mount(SettingsTriggersView, {
      global: { stubs: { ...dialogStubs.reduce((a, k) => ({ ...a, [k]: true }), {}), FeatureGate: featureGateStub } },
    })
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })

  it('FAR-169: rejects a polling interval below 60 client-side without calling the API', async () => {
    const wrapper = mountView(fakeJwt('admin'), baseListData)
    await flush()
    const vm = wrapper.vm as any
    vm.form = { ...vm.defaultForm, pipeline_id: 'p1', trigger_type: 'polling', poll_interval: 30 }
    await vm.saveTrigger()
    await flush()
    expect(vm.formError).toContain('at least 60')
    expect(api.POST).not.toHaveBeenCalled()
  })

  it('FAR-169: polling interval input enforces a 60s floor (min=60) with the cadence hint', async () => {
    ;(api.GET as any).mockResolvedValue({ data: baseListData, error: undefined })
    ;(getAccessToken as any).mockReturnValue(fakeJwt('admin'))
    const wrapper = mount(SettingsTriggersView, {
      global: {
        stubs: {
          ...dialogStubs.reduce((a, k) => ({ ...a, [k]: true }), {}),
          FeatureGate: featureGateStub,
          FormDialog: { template: '<div><slot /></div>' },
        },
      },
    })
    await flush()
    const vm = wrapper.vm as any
    vm.form = { ...vm.defaultForm, pipeline_id: 'p1', trigger_type: 'polling' }
    await nextTick()

    const input = wrapper.find('[data-testid="settings-triggers-form-polling-interval"]')
    expect(input.exists()).toBe(true)
    expect(input.attributes('min')).toBe('60')
    expect(wrapper.text()).toContain('Minimum 60 seconds')
  })

  it('renders webhook and cron JSON placeholders without i18n message-compile errors', async () => {
    ;(api.GET as any).mockResolvedValue({ data: baseListData, error: undefined })
    ;(getAccessToken as any).mockReturnValue(fakeJwt('admin'))
    const wrapper = mount(SettingsTriggersView, {
      global: {
        stubs: {
          ...dialogStubs.reduce((a, k) => ({ ...a, [k]: true }), {}),
          FeatureGate: featureGateStub,
          FormDialog: { template: '<div><slot /></div>' },
        },
      },
    })
    await flush()
    const vm = wrapper.vm as any

    vm.form = { ...vm.defaultForm, pipeline_id: 'p1', trigger_type: 'webhook' }
    await nextTick()
    const headers = wrapper.find('[data-testid="settings-triggers-form-webhook-headers"]')
    expect(headers.attributes('placeholder')).toContain('X-Custom-Header')

    vm.form = { ...vm.defaultForm, pipeline_id: 'p1', trigger_type: 'cron' }
    await nextTick()
    const tpl = wrapper.find('[data-testid="settings-triggers-form-cron-input"]')
    expect(tpl.attributes('placeholder')).toContain('"key"')
  })
})
