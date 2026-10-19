import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick as vueNextTick } from 'vue'

const { mockGet, mockPut } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPut: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    PUT: mockPut,
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminEvidenceRedactionView from '../views/AdminEvidenceRedactionView.vue'

interface Rule {
  id: string
  key_pattern: string
  role: string
  redacted_fields: string[]
  created_at: string
  updated_at: string
}

const CATCH_ALL: Rule = {
  id: 'rule-catch-all',
  key_pattern: '*',
  role: 'viewer',
  redacted_fields: ['sensitive_output', 'internal_metadata'],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-02T00:00:00Z',
}

const SCOPED: Rule = {
  id: 'rule-scoped',
  key_pattern: 'eval.*',
  role: 'operator',
  redacted_fields: ['internal_metadata'],
  created_at: '2026-01-03T00:00:00Z',
  updated_at: '2026-01-04T00:00:00Z',
}

async function settle(): Promise<void> {
  await vueNextTick()
  await flushPromises()
  await vueNextTick()
}

function mountView() {
  return mount(AdminEvidenceRedactionView, {
    global: {
      stubs: {
        LoadingSpinner: true,
      },
    },
  })
}

function mockRules(rules: Rule[]): void {
  mockGet.mockResolvedValue({ data: rules, error: undefined })
}

describe('AdminEvidenceRedactionView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPut.mockResolvedValue({ data: { ...CATCH_ALL }, error: undefined })
    mockRules([CATCH_ALL, SCOPED])
  })

  it('lists the org rules with key patterns, roles and redacted classes', async () => {
    const wrapper = mountView()
    await settle()

    expect(wrapper.find('[data-testid="admin-evidence-redaction-table"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('*')
    expect(wrapper.text()).toContain('eval.*')
    expect(wrapper.text()).toContain('Operator')
    expect(wrapper.text()).toContain('viewer')
    expect(wrapper.text()).toContain('sensitive_output')
    expect(wrapper.text()).toContain('internal_metadata')
  })

  it('marks the seeded * / viewer rule as the mandatory catch-all and explains it', async () => {
    const wrapper = mountView()
    await settle()

    const badge = wrapper.find('[data-testid="admin-evidence-redaction-catch-all"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('Mandatory')
    expect(wrapper.find('[data-testid="admin-evidence-redaction-catch-all-note"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-evidence-redaction-catch-all-missing"]').exists()).toBe(false)
  })

  it('creates a rule with the exact snake_case RedactionRuleUpsert payload', async () => {
    const wrapper = mountView()
    await settle()

    await wrapper.find('[data-testid="admin-evidence-redaction-add"]').trigger('click')
    await settle()

    await wrapper.find('[data-testid="admin-evidence-redaction-key-pattern"]').setValue('evidence.*')
    await wrapper.find('[data-testid="admin-evidence-redaction-role"]').setValue('admin')
    const classBoxes = wrapper.findAll('[data-testid="admin-evidence-redaction-field-checkbox"]')
    expect(classBoxes).toHaveLength(2)
    await classBoxes[0].setValue(true)

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).toHaveBeenCalledTimes(1)
    expect(mockPut.mock.calls[0][0]).toBe('/api/v1/evidence/redaction-rules')
    const body = mockPut.mock.calls[0][1].body
    expect(body).toEqual({
      key_pattern: 'evidence.*',
      role: 'admin',
      redacted_fields: ['sensitive_output'],
    })
    expect(Object.keys(body).sort()).toEqual(['key_pattern', 'redacted_fields', 'role'])
  })

  it('rejects an empty key pattern without calling the API', async () => {
    const wrapper = mountView()
    await settle()

    await wrapper.find('[data-testid="admin-evidence-redaction-add"]').trigger('click')
    await settle()

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).not.toHaveBeenCalled()
    const error = wrapper.find('[data-testid="admin-evidence-redaction-form-error"]')
    expect(error.exists()).toBe(true)
    expect(error.attributes('aria-live')).toBe('assertive')
    expect(error.text()).toContain('Enter a key pattern')
  })

  it('edits an existing (key_pattern, role) rule in place and keeps its identity fixed', async () => {
    const wrapper = mountView()
    await settle()

    // The catch-all row is first; the scoped rule is the second row.
    const editButtons = wrapper.findAll('[data-testid="admin-evidence-redaction-edit"]')
    expect(editButtons).toHaveLength(2)
    await editButtons[1].trigger('click')
    await settle()

    const keyInput = wrapper.find('[data-testid="admin-evidence-redaction-key-pattern"]')
    const roleSelect = wrapper.find('[data-testid="admin-evidence-redaction-role"]')
    expect((keyInput.element as HTMLInputElement).value).toBe('eval.*')
    expect((keyInput.element as HTMLInputElement).disabled).toBe(true)
    expect((roleSelect.element as HTMLSelectElement).value).toBe('operator')
    expect((roleSelect.element as HTMLSelectElement).disabled).toBe(true)

    // internal_metadata is checked from the stored rule; add sensitive_output.
    const classBoxes = wrapper.findAll('[data-testid="admin-evidence-redaction-field-checkbox"]')
    await classBoxes[0].setValue(true)

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).toHaveBeenCalledTimes(1)
    const body = mockPut.mock.calls[0][1].body
    // The backend normalises the list with sorted(set(...)), so order is not
    // part of the contract — the SET of classes is.
    expect({
      ...body,
      redacted_fields: [...body.redacted_fields].sort(),
    }).toEqual({
      key_pattern: 'eval.*',
      role: 'operator',
      redacted_fields: ['internal_metadata', 'sensitive_output'],
    })
  })

  it('shows a loud warning when the mandatory catch-all row is absent', async () => {
    mockRules([SCOPED])
    const wrapper = mountView()
    await settle()

    expect(wrapper.find('[data-testid="admin-evidence-redaction-catch-all"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="admin-evidence-redaction-catch-all-missing"]').exists()).toBe(true)
  })
})
