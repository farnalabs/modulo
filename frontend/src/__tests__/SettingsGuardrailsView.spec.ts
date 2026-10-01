import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
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
// The confirm button renders only while the dialog is open, mirroring the real
// FormDialog, so a test can open one dialog and see exactly one confirm button.
const formDialogStub = {
  name: 'FormDialog',
  props: ['open', 'title', 'description', 'confirmText', 'loading', 'confirmDisabled'],
  emits: ['update:open', 'confirm'],
  template: '<div><slot /><button v-if="open" class="formdialog-confirm" @click="$emit(\'confirm\')">{{ confirmText }}</button></div>',
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
    expect(badge.attributes('aria-live')).toBe('polite')
  })

  it('does not show the observe badge for block/warn/redact guardrails', async () => {
    const wrapper = mountView(fakeJwt('admin'), {
      list: {
        items: [
          guardrailItem({ name: 'block_cc', config_json: { action: 'block', type: 'regex', field: 'payload.card_number', pattern: '^4' } }),
          guardrailItem({ id: 'gr-2', name: 'warn_secrets', config_json: { action: 'warn', type: 'regex', field: 'payload.secret', pattern: 'sk-' } }),
          guardrailItem({ id: 'gr-3', name: 'redact_pii', config_json: { action: 'redact', type: 'json_schema', field: 'payload.email' } }),
        ],
        total: 3,
        page: 1,
        page_size: 100,
      },
    })
    await flush()

    expect(wrapper.find('[data-testid="settings-guardrails-observe-badge"]').exists()).toBe(false)
  })

  it('lists guardrails by field name only — never renders configured values', async () => {
    const pattern = '^4[0-9]{12}(?:[0-9]{3})?$'
    const wrapper = mountView(fakeJwt('admin'), {
      list: {
        items: [guardrailItem({ name: 'block_credit_card' })],
        total: 1,
        page: 1,
        page_size: 100,
      },
    })
    await flush()

    // The field NAME is rendered...
    expect(wrapper.text()).toContain('payload.card_number')
    // ...but the actual configured pattern value must never surface.
    expect(wrapper.text()).not.toContain(pattern)
  })

  it('shows the kill-switch banner for non-admin org members via the org-scoped read', async () => {
    const wrapper = mountView(fakeJwt('viewer'), {
      list: { items: [guardrailItem()], total: 1, page: 1, page_size: 100 },
      killSwitch: { enabled: true },
    })
    await flush()

    const banner = wrapper.find('[data-testid="settings-guardrails-kill-switch-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('aria-live')).toBe('polite')
    // Non-admins read the org-scoped endpoint (not the admin-only one).
    expect(getMock).toHaveBeenCalledWith('/api/v1/org/settings/guardrails/kill-switch')
  })

  it('regex detection requires a pattern before POSTing', async () => {
    const wrapper = mountView(fakeJwt('admin'))
    await flush()

    ;(wrapper.vm as any).form.name = 'block_cc'
    ;(wrapper.vm as any).form.pipeline_id = 'p1'
    ;(wrapper.vm as any).form.action = 'block'
    ;(wrapper.vm as any).form.detectionType = 'regex'
    ;(wrapper.vm as any).form.field = 'payload.card_number'
    ;(wrapper.vm as any).form.pattern = ''

    await wrapper.find('form').trigger('submit')
    await flush()

    const error = wrapper.find('[data-testid="settings-guardrails-form-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('pattern')
    expect(api.POST).not.toHaveBeenCalled()
  })

  it('json_schema detection requires valid JSON schema before POSTing', async () => {
    const wrapper = mountView(fakeJwt('admin'))
    await flush()

    ;(wrapper.vm as any).form.name = 'schema_guard'
    ;(wrapper.vm as any).form.pipeline_id = 'p1'
    ;(wrapper.vm as any).form.action = 'block'
    ;(wrapper.vm as any).form.detectionType = 'json_schema'
    ;(wrapper.vm as any).form.field = 'payload.record'
    ;(wrapper.vm as any).form.schema = 'not-json'

    await wrapper.find('form').trigger('submit')
    await flush()

    const error = wrapper.find('[data-testid="settings-guardrails-form-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('JSON')
    expect(api.POST).not.toHaveBeenCalled()
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

  it('shows the Import Config control for admins only', async () => {
    const admin = mountView(fakeJwt('admin'))
    await flush()
    expect(admin.find('[data-testid="settings-guardrails-import"]').exists()).toBe(true)

    const viewer = mountView(fakeJwt('viewer'))
    await flush()
    expect(viewer.find('[data-testid="settings-guardrails-import"]').exists()).toBe(false)
  })

  it('opens the import dialog and clears prior import state', async () => {
    const wrapper = mountView(fakeJwt('admin'))
    await flush()

    ;(wrapper.vm as any).importSuccess = 'stale'
    await wrapper.find('[data-testid="settings-guardrails-import"]').trigger('click')
    await flush()

    expect(wrapper.find('[data-testid="settings-guardrails-import-dialog"]').exists()).toBe(true)
    expect((wrapper.vm as any).importOpen).toBe(true)
    expect((wrapper.vm as any).importYaml).toBe('')
    expect((wrapper.vm as any).importError).toBe(null)
    expect((wrapper.vm as any).importSuccess).toBe(null)
  })

  it('does not POST when the import YAML is empty', async () => {
    const wrapper = mountView(fakeJwt('admin'))
    await flush()

    await wrapper.find('[data-testid="settings-guardrails-import"]').trigger('click')
    await flush()
    ;(wrapper.vm as any).importYaml = '   '

    await wrapper.find('.formdialog-confirm').trigger('click')
    await flush()

    expect(api.POST).not.toHaveBeenCalled()
  })

  it('imports a config YAML as the applied state and refreshes the guardrail list', async () => {
    ;(api.POST as any).mockResolvedValue({
      data: {
        imported: true,
        hash: 'abc123def',
        applied_at: '2026-10-01T00:00:00Z',
        status: 'clean',
        diff: [{ action: 'add', id: 'g1' }, { action: 'remove', id: 'g2' }],
      },
      error: undefined,
    })
    const wrapper = mountView(fakeJwt('admin'))
    await flush()
    expect((api.GET as any).mock.calls.filter(([url]: string[]) => url === '/api/v1/evals').length).toBe(1)

    await wrapper.find('[data-testid="settings-guardrails-import"]').trigger('click')
    await flush()
    ;(wrapper.vm as any).importYaml = 'guardrails:\n  - id: g1'

    await wrapper.find('.formdialog-confirm').trigger('click')
    await flush()
    await flushPromises()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/guardrails/config/import', {
      body: { config_yaml: 'guardrails:\n  - id: g1' },
    })
    const success = wrapper.find('[data-testid="settings-guardrails-import-success"]')
    expect(success.exists()).toBe(true)
    expect(success.text()).toContain('abc123def')
    // The list refresh after a successful import re-fetches /api/v1/evals.
    expect((api.GET as any).mock.calls.filter(([url]: string[]) => url === '/api/v1/evals').length).toBe(2)
  })

  it('surfaces an import API error in the dialog without refreshing the list', async () => {
    ;(api.POST as any).mockResolvedValue({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:conflict',
        title: 'Conflict',
        status: 409,
        detail: 'Cannot import guardrail config: id(s) collide with node-bound guardrails: g1',
      },
    })
    const wrapper = mountView(fakeJwt('admin'))
    await flush()

    await wrapper.find('[data-testid="settings-guardrails-import"]').trigger('click')
    await flush()
    ;(wrapper.vm as any).importYaml = 'guardrails:\n  - id: g1'

    await wrapper.find('.formdialog-confirm').trigger('click')
    await flush()
    await flushPromises()

    const error = wrapper.find('[data-testid="settings-guardrails-import-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('collide')
    expect(wrapper.find('[data-testid="settings-guardrails-import-success"]').exists()).toBe(false)
    expect((api.GET as any).mock.calls.filter(([url]: string[]) => url === '/api/v1/evals').length).toBe(1)
  })

  it('ignores a close request while an import is in flight', async () => {
    const wrapper = mountView(fakeJwt('admin'))
    await flush()

    await wrapper.find('[data-testid="settings-guardrails-import"]').trigger('click')
    await flush()

    // A close request while a request is in flight must not dismiss the dialog.
    ;(wrapper.vm as any).importing = true
    ;(wrapper.vm as any).closeImportDialog(false)
    expect((wrapper.vm as any).importOpen).toBe(true)

    // An explicit open request is honoured even while importing.
    ;(wrapper.vm as any).closeImportDialog(true)
    expect((wrapper.vm as any).importOpen).toBe(true)

    // Once the import settles, a close request dismisses the dialog.
    ;(wrapper.vm as any).importing = false
    ;(wrapper.vm as any).closeImportDialog(false)
    expect((wrapper.vm as any).importOpen).toBe(false)
  })

  it('reports a zero-change import when the response omits diff and hash', async () => {
    ;(api.POST as any).mockResolvedValue({
      data: { imported: true, status: 'clean' },
      error: undefined,
    })
    const wrapper = mountView(fakeJwt('admin'))
    await flush()

    await wrapper.find('[data-testid="settings-guardrails-import"]').trigger('click')
    await flush()
    ;(wrapper.vm as any).importYaml = 'guardrails: []'

    await wrapper.find('.formdialog-confirm').trigger('click')
    await flush()
    await flushPromises()

    const success = wrapper.find('[data-testid="settings-guardrails-import-success"]')
    expect(success.exists()).toBe(true)
    expect(success.text()).toContain('0 change(s)')
  })

  it('surfaces a thrown import failure without refreshing the list', async () => {
    ;(api.POST as any).mockRejectedValue(new Error('network down'))
    const wrapper = mountView(fakeJwt('admin'))
    await flush()
    expect((api.GET as any).mock.calls.filter(([url]: string[]) => url === '/api/v1/evals').length).toBe(1)

    await wrapper.find('[data-testid="settings-guardrails-import"]').trigger('click')
    await flush()
    ;(wrapper.vm as any).importYaml = 'guardrails:\n  - id: g1'

    await wrapper.find('.formdialog-confirm').trigger('click')
    await flush()
    await flushPromises()

    const error = wrapper.find('[data-testid="settings-guardrails-import-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('network down')
    expect((wrapper.vm as any).importing).toBe(false)
    expect((api.GET as any).mock.calls.filter(([url]: string[]) => url === '/api/v1/evals').length).toBe(1)
  })
})
