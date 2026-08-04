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
