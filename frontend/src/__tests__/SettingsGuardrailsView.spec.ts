import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }))

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
  useApi: () => ({ put: vi.fn(), get: getMock, post: vi.fn(), delete: vi.fn(), patch: vi.fn() }),
}))

vi.mock('../lib/api/schema', () => ({}))

import SettingsGuardrailsView from '../views/SettingsGuardrailsView.vue'
import { api, getAccessToken } from '../lib/api/client'

const featureGateStub = { template: '<div><slot /></div>' }
// Pass-through stub so the creation form's slot always renders for testing.
const formDialogStub = {
  name: 'FormDialog',
  props: ['open', 'title', 'description', 'confirmText', 'loading'],
  emits: ['update:open', 'confirm'],
  template: '<div><slot /></div>',
}
const selectStub = { template: '<div><slot /></div>' }

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

function guardrailItem(overrides: Record<string, unknown> = {}) {
  return {
    id: 'gr-1',
    pipeline_id: 'p1',
    node_id: null,
    name: 'block_credit_card',
    eval_type: 'guardrail',
    config_json: {
      interception_point: 'input',
      action: 'block',
      type: 'regex',
      field: 'payload.card_number',
      pattern: '^4[0-9]{12}$',
    },
    failure_behaviour: 'warn',
    ...overrides,
  }
}

function mountView(token: string, overrides: { list?: unknown; killSwitch?: unknown } = {}) {
  ;(getAccessToken as any).mockReturnValue(token)
  ;(api.GET as any).mockImplementation((url: string) => {
    if (url === '/api/v1/evals') {
      return Promise.resolve({ data: overrides.list ?? { items: [], total: 0, page: 1, page_size: 100 }, error: undefined })
    }
    if (url === '/api/v1/pipelines') {
      return Promise.resolve({ data: { items: [{ id: 'p1', name: 'My Pipeline' }], total: 1 }, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
  getMock.mockResolvedValue(overrides.killSwitch ?? { enabled: false })
  return mount(SettingsGuardrailsView, {
    global: {
      stubs: {
        FeatureGate: featureGateStub,
        FormDialog: formDialogStub,
        Select: selectStub,
        SelectTrigger: true,
        SelectContent: true,
        SelectItem: true,
        SelectValue: true,
        LoadingSpinner: { template: '<div />' },
        PageHeader: { template: '<div />' },
      },
    },
  })
}

async function flush() {
  await nextTick()
  await nextTick()
  await nextTick()
}

describe('SettingsGuardrailsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders the guardrail list rows from eval definitions', async () => {
    const wrapper = mountView(fakeJwt('admin'), {
      list: {
        items: [
          guardrailItem({ name: 'block_credit_card' }),
          guardrailItem({ id: 'gr-2', name: 'observe_secrets', config_json: { action: 'observe', type: 'regex', field: 'payload.secret', pattern: 'sk-' } }),
        ],
        total: 2,
        page: 1,
        page_size: 100,
      },
    })
    await flush()

    expect(wrapper.find('[data-testid="settings-guardrails-create"]').exists()).toBe(true)
    const rows = wrapper.findAll('tbody tr')
    expect(rows.length).toBe(2)
    expect(wrapper.text()).toContain('block_credit_card')
    expect(wrapper.text()).toContain('observe_secrets')
    expect(wrapper.text()).toContain('My Pipeline')
  })

  it('shows the observe badge for observe-mode guardrails', async () => {
    const wrapper = mountView(fakeJwt('admin'), {
      list: {
        items: [guardrailItem({ name: 'observe_secrets', config_json: { action: 'observe', type: 'regex', field: 'payload.secret', pattern: 'sk-' } })],
        total: 1,
        page: 1,
        page_size: 100,
      },
    })
    await flush()

    const badge = wrapper.find('[data-testid="settings-guardrails-observe-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.attributes('role')).toBe('status')
    expect(badge.attributes('aria-live')).toBe('polite')
  })

  it('shows the kill-switch banner when the org kill-switch is ON and downgrades block guardrails to observe', async () => {
    const wrapper = mountView(fakeJwt('admin'), {
      list: {
        items: [guardrailItem({ name: 'block_credit_card' })],
        total: 1,
        page: 1,
        page_size: 100,
      },
      killSwitch: { enabled: true },
    })
    await flush()

    const banner = wrapper.find('[data-testid="settings-guardrails-kill-switch-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('role')).toBe('status')
    expect(banner.attributes('aria-live')).toBe('polite')
    // The block-action guardrail is downgraded to observe while the kill-switch is ON.
    expect(wrapper.find('[data-testid="settings-guardrails-observe-badge"]').exists()).toBe(true)
  })

  it('does not show the kill-switch banner when OFF', async () => {
    const wrapper = mountView(fakeJwt('admin'), {
      list: { items: [guardrailItem()], total: 1, page: 1, page_size: 100 },
      killSwitch: { enabled: false },
    })
    await flush()

    expect(wrapper.find('[data-testid="settings-guardrails-kill-switch-banner"]').exists()).toBe(false)
  })

  it('creation form validates required fields before POSTing', async () => {
    const wrapper = mountView(fakeJwt('admin'))
    await flush()

    // Submit without filling required fields.
    const form = wrapper.find('form')
    await form.trigger('submit')
    await flush()

    const error = wrapper.find('[data-testid="settings-guardrails-form-error"]')
    expect(error.exists()).toBe(true)
    expect(api.POST).not.toHaveBeenCalled()
  })

  it('creation form includes the point-of-decision disclosure note', async () => {
    const wrapper = mountView(fakeJwt('admin'))
    await flush()

    expect(wrapper.find('[data-testid="settings-guardrails-form-disclosure"]').exists()).toBe(true)
  })
})
