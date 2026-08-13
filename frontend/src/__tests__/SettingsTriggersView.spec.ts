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
})

describe('SettingsTriggersView — ongoing trigger (FAR-158)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  const ongoingTrigger = {
    id: 'trig-ongoing-1',
    pipeline_id: 'p1',
    trigger_type: 'ongoing',
    active: true,
    max_concurrent_runs: 4,
    daily_spend_limit: 50,
    config_json: { scan_interval_seconds: 300, input_template: { topic: 'security' } },
  }

  // The ``true`` dialog stubs do not render their default slot; the ongoing
  // form lives inside the FormDialog, so use slot-rendering stubs here.
  const slotStub = { template: '<div><slot /></div>' }
  const slotStubs = {
    Dialog: slotStub,
    DialogContent: slotStub,
    DialogDescription: slotStub,
    DialogFooter: slotStub,
    DialogHeader: slotStub,
    DialogTitle: slotStub,
    FeatureGate: featureGateStub,
  }

  function mountWithApi(token: string, listData: Record<string, unknown>) {
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url.includes('/pipelines')) {
        return Promise.resolve({ data: { items: [{ id: 'p1', name: 'P1 Pipeline' }], total: 1 }, error: undefined })
      }
      return Promise.resolve({ data: listData, error: undefined })
    })
    ;(api.POST as any).mockResolvedValue({ data: null, error: undefined })
    ;(api.PUT as any).mockResolvedValue({ data: null, error: undefined })
    ;(getAccessToken as any).mockReturnValue(token)
    return mount(SettingsTriggersView, { global: { stubs: slotStubs } })
  }

  it('create: POST body carries exact snake_case keys for the ongoing form', async () => {
    const wrapper = mountWithApi(fakeJwt('admin'), { ...baseListData, triggers_paused: false, paused_at: null })
    await flush()

    await wrapper.find('[data-testid="settings-triggers-create"]').trigger('click')
    await flush()

    // Select 'ongoing' in the type dropdown (setupState-backed form binding).
    ;(wrapper.vm as any).form.trigger_type = 'ongoing'
    ;(wrapper.vm as any).form.pipeline_id = 'p1'
    await flush()

    // The ongoing form controls are rendered and carry the expected testids.
    expect(wrapper.find('[data-testid="settings-triggers-form-ongoing-target"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-triggers-form-ongoing-interval"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-triggers-form-ongoing-template"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-triggers-form-ongoing-spend"]').exists()).toBe(true)

    await wrapper.find('[data-testid="settings-triggers-form-ongoing-target"]').setValue(3)
    await wrapper.find('[data-testid="settings-triggers-form-ongoing-interval"]').setValue(120)
    await wrapper.find('[data-testid="settings-triggers-form-ongoing-template"]').setValue('{"topic": "security"}')
    await wrapper.find('[data-testid="settings-triggers-form-ongoing-spend"]').setValue(25)
    await flush()

    await wrapper.find('form').trigger('submit')
    await flush()

    expect(api.POST).toHaveBeenCalledTimes(1)
    const [url, options] = (api.POST as any).mock.calls[0]
    expect(url).toBe('/api/v1/pipelines/{pipeline_id}/triggers')
    expect(options.body).toEqual({
      trigger_type: 'ongoing',
      active: true,
      config_json: { scan_interval_seconds: 120, input_template: { topic: 'security' } },
      max_concurrent_runs: 3,
      daily_spend_limit: 25,
    })
  })

  it('create: saving without a daily spend limit shows a form error and no POST fires', async () => {
    const wrapper = mountWithApi(fakeJwt('admin'), { ...baseListData, triggers_paused: false, paused_at: null })
    await flush()

    await wrapper.find('[data-testid="settings-triggers-create"]').trigger('click')
    await flush()
    ;(wrapper.vm as any).form.trigger_type = 'ongoing'
    ;(wrapper.vm as any).form.pipeline_id = 'p1'
    await flush()

    await wrapper.find('[data-testid="settings-triggers-form-ongoing-target"]').setValue(3)
    await wrapper.find('[data-testid="settings-triggers-form-ongoing-interval"]').setValue(120)
    await flush()

    await wrapper.find('form').trigger('submit')
    await flush()

    expect(api.POST).not.toHaveBeenCalled()
    expect(wrapper.find('.text-destructive').exists()).toBe(true)
  })

  it('edit: the form pre-fills target / interval / spend from the loaded ongoing trigger', async () => {
    const wrapper = mountWithApi(fakeJwt('admin'), { ...baseListData, items: [ongoingTrigger] })
    await flush()

    ;(wrapper.vm as any).openEditDialog(ongoingTrigger)
    await flush()

    const target = wrapper.find('[data-testid="settings-triggers-form-ongoing-target"]')
    const interval = wrapper.find('[data-testid="settings-triggers-form-ongoing-interval"]')
    const spend = wrapper.find('[data-testid="settings-triggers-form-ongoing-spend"]')
    expect(target.exists()).toBe(true)
    expect((target.element as HTMLInputElement).value).toBe('4')
    expect((interval.element as HTMLInputElement).value).toBe('300')
    expect((spend.element as HTMLInputElement).value).toBe('50')
  })

  it('edit: PUT preserves existing config keys (merge, never wipe)', async () => {
    const wrapper = mountWithApi(fakeJwt('admin'), { ...baseListData, items: [ongoingTrigger] })
    await flush()

    ;(wrapper.vm as any).openEditDialog(ongoingTrigger)
    await flush()
    await wrapper.find('form').trigger('submit')
    await flush()

    expect(api.PUT).toHaveBeenCalledTimes(1)
    const [url, options] = (api.PUT as any).mock.calls[0]
    expect(url).toBe('/api/v1/triggers/{trigger_id}')
    expect(options.body.max_concurrent_runs).toBe(4)
    expect(options.body.daily_spend_limit).toBe('50')
    expect(options.body.config_json.scan_interval_seconds).toBe(300)
    expect(options.body.config_json.input_template).toEqual({ topic: 'security' })
  })
})
