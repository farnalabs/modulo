import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick as vueNextTick } from 'vue'
import type { components } from '../lib/api/schema'

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

type RedactionRule = components['schemas']['RedactionRuleResponse']

const LIST_PATH = '/api/v1/evidence/redaction-rules'

const CATCH_ALL: RedactionRule = {
  id: 'rule-catch-all',
  key_pattern: '*',
  role: 'viewer',
  redacted_fields: ['sensitive_output', 'internal_metadata'],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-02T00:00:00Z',
}

const SCOPED: RedactionRule = {
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

function mockRules(rules: RedactionRule[]): void {
  mockGet.mockResolvedValue({ data: rules, error: undefined })
}

function ruleRows(wrapper: ReturnType<typeof mountView>) {
  return wrapper.findAll('[data-testid="admin-evidence-redaction-table"] tbody tr')
}

async function openCreateForm(wrapper: ReturnType<typeof mountView>): Promise<void> {
  await wrapper.find('[data-testid="admin-evidence-redaction-add"]').trigger('click')
  await settle()
}

async function fillRequiredFields(
  wrapper: ReturnType<typeof mountView>,
  keyPattern: string,
  role: string,
): Promise<void> {
  await wrapper.find('[data-testid="admin-evidence-redaction-key-pattern"]').setValue(keyPattern)
  await wrapper.find('[data-testid="admin-evidence-redaction-role"]').setValue(role)
}

describe('AdminEvidenceRedactionView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPut.mockResolvedValue({ data: { ...CATCH_ALL }, error: undefined })
    mockRules([CATCH_ALL, SCOPED])
  })

  it('lists the org rules from the exact endpoint, with each row scoped to its cells', async () => {
    const wrapper = mountView()
    await settle()

    expect(mockGet).toHaveBeenCalledTimes(1)
    expect(mockGet).toHaveBeenCalledWith(LIST_PATH)
    expect(wrapper.find('[data-testid="admin-evidence-redaction-table"]').exists()).toBe(true)

    const rows = ruleRows(wrapper)
    expect(rows).toHaveLength(2)

    const catchAllCells = rows[0].findAll('td')
    expect(catchAllCells).toHaveLength(5)
    expect(catchAllCells[0].text()).toContain('*')
    expect(
      catchAllCells[0].find('[data-testid="admin-evidence-redaction-catch-all"]').exists(),
    ).toBe(true)
    expect(catchAllCells[1].text()).toBe('Viewer')
    expect(catchAllCells[2].text()).toContain('sensitive_output')
    expect(catchAllCells[2].text()).toContain('internal_metadata')
    expect(catchAllCells[3].text()).toContain('2026')

    const scopedCells = rows[1].findAll('td')
    expect(scopedCells[0].text()).toContain('eval.*')
    expect(scopedCells[1].text()).toBe('Operator')
    expect(scopedCells[2].text()).toContain('internal_metadata')
  })

  it('marks the seeded * / viewer rule as the mandatory catch-all and explains its scope', async () => {
    const wrapper = mountView()
    await settle()

    const badge = wrapper.find('[data-testid="admin-evidence-redaction-catch-all"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('Mandatory')

    const note = wrapper.find('[data-testid="admin-evidence-redaction-catch-all-note"]')
    expect(note.exists()).toBe(true)
    expect(note.text()).toContain('applies only to the viewer role')
    expect(note.text()).toContain('runner')
    expect(wrapper.find('[data-testid="admin-evidence-redaction-catch-all-missing"]').exists()).toBe(
      false,
    )
  })

  it('creates a rule with the exact snake_case RedactionRuleUpsert payload', async () => {
    const wrapper = mountView()
    await settle()

    await openCreateForm(wrapper)
    await fillRequiredFields(wrapper, 'evidence.*', 'admin')
    const classBoxes = wrapper.findAll('[data-testid="admin-evidence-redaction-field-checkbox"]')
    expect(classBoxes).toHaveLength(2)
    await classBoxes[0].setValue(true)

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).toHaveBeenCalledTimes(1)
    expect(mockPut.mock.calls[0][0]).toBe(LIST_PATH)
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

    await openCreateForm(wrapper)
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).not.toHaveBeenCalled()
    const error = wrapper.find('[data-testid="admin-evidence-redaction-form-error"]')
    expect(error.exists()).toBe(true)
    expect(error.attributes('aria-live')).toBe('assertive')
    expect(error.text()).toContain('Enter a key pattern')
  })

  it('rejects a rule with zero redacted classes without issuing the PUT', async () => {
    const wrapper = mountView()
    await settle()

    await openCreateForm(wrapper)
    await fillRequiredFields(wrapper, 'evidence.none', 'admin')
    // No class checkbox ticked: an empty set would silently disable redaction.
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).not.toHaveBeenCalled()
    const error = wrapper.find('[data-testid="admin-evidence-redaction-form-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('Select at least one redacted class')
    // The form stays open so the admin can correct it.
    expect(wrapper.find('[data-testid="admin-evidence-redaction-form-title"]').exists()).toBe(true)
  })

  it('refuses to create over an existing (key_pattern, role) pair and points at Edit', async () => {
    const wrapper = mountView()
    await settle()

    await openCreateForm(wrapper)
    // The seeded catch-all pair already exists.
    await fillRequiredFields(wrapper, '*', 'viewer')
    const classBoxes = wrapper.findAll('[data-testid="admin-evidence-redaction-field-checkbox"]')
    await classBoxes[0].setValue(true)

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).not.toHaveBeenCalled()
    const error = wrapper.find('[data-testid="admin-evidence-redaction-form-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('already exists')
    expect(error.text()).toContain('Use Edit')
    expect(wrapper.find('[data-testid="admin-evidence-redaction-form-title"]').exists()).toBe(true)
  })

  it('edits an existing rule in place and keeps its identity fixed', async () => {
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
    expect(Object.keys(body).sort()).toEqual(['key_pattern', 'redacted_fields', 'role'])
  })

  it('keeps an unrecognised stored role read-only and disables Save', async () => {
    mockRules([{ ...SCOPED, role: 'runner' }])
    const wrapper = mountView()
    await settle()

    await wrapper.find('[data-testid="admin-evidence-redaction-edit"]').trigger('click')
    await settle()

    const roleSelect = wrapper.find('[data-testid="admin-evidence-redaction-role"]')
    expect((roleSelect.element as HTMLSelectElement).value).toBe('runner')
    expect((roleSelect.element as HTMLSelectElement).disabled).toBe(true)
    expect(
      wrapper.find('[data-testid="admin-evidence-redaction-role-unrecognised"]').exists(),
    ).toBe(true)

    const save = wrapper.find('[data-testid="admin-evidence-redaction-save"]')
    expect(save.attributes('disabled')).toBeDefined()

    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).not.toHaveBeenCalled()
  })

  it('keeps an unrecognised stored class visible so it can be removed', async () => {
    mockRules([{ ...SCOPED, redacted_fields: ['sensitive_output', 'legacy_class'] }])
    const wrapper = mountView()
    await settle()

    await wrapper.find('[data-testid="admin-evidence-redaction-edit"]').trigger('click')
    await settle()

    const classBoxes = wrapper.findAll('[data-testid="admin-evidence-redaction-field-checkbox"]')
    expect(classBoxes).toHaveLength(3)
    const legacyBox = classBoxes[2]
    expect((legacyBox.element as HTMLInputElement).value).toBe('legacy_class')
    expect((legacyBox.element as HTMLInputElement).checked).toBe(true)

    // Remove the class this build does not know about, then save the rule.
    await legacyBox.setValue(false)
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).toHaveBeenCalledTimes(1)
    expect(mockPut.mock.calls[0][1].body.redacted_fields).toEqual(['sensitive_output'])
  })

  it('treats an initial load failure as page-fatal and recovers on retry', async () => {
    mockGet.mockResolvedValueOnce({ data: undefined, error: { detail: 'registry unavailable' } })
    const wrapper = mountView()
    await settle()

    const loadError = wrapper.find('[data-testid="admin-evidence-redaction-load-error"]')
    expect(loadError.exists()).toBe(true)
    expect(loadError.text()).toContain('registry unavailable')
    expect(wrapper.find('[data-testid="admin-evidence-redaction-table"]').exists()).toBe(false)
    expect(
      wrapper.find('[data-testid="admin-evidence-redaction-add"]').attributes('disabled'),
    ).toBeDefined()

    mockGet.mockResolvedValueOnce({ data: [CATCH_ALL], error: undefined })
    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await settle()

    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-testid="admin-evidence-redaction-load-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="admin-evidence-redaction-table"]').exists()).toBe(true)
    expect(ruleRows(wrapper)).toHaveLength(1)
  })

  it('keeps the form open and shows the API error when the save fails', async () => {
    mockPut.mockResolvedValueOnce({ data: undefined, error: { detail: 'Rule rejected' } })
    const wrapper = mountView()
    await settle()

    await openCreateForm(wrapper)
    await fillRequiredFields(wrapper, 'evidence.*', 'admin')
    const classBoxes = wrapper.findAll('[data-testid="admin-evidence-redaction-field-checkbox"]')
    await classBoxes[0].setValue(true)
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockPut).toHaveBeenCalledTimes(1)
    const error = wrapper.find('[data-testid="admin-evidence-redaction-form-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('Rule rejected')
    expect(wrapper.find('[data-testid="admin-evidence-redaction-form-title"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-evidence-redaction-status"]').exists()).toBe(false)
  })

  it('refetches after a successful save and announces the success politely', async () => {
    const wrapper = mountView()
    await settle()

    await openCreateForm(wrapper)
    await fillRequiredFields(wrapper, 'evidence.*', 'admin')
    const classBoxes = wrapper.findAll('[data-testid="admin-evidence-redaction-field-checkbox"]')
    await classBoxes[0].setValue(true)
    await wrapper.find('form').trigger('submit')
    await settle()

    // A post-save refetch re-reads the list.
    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(mockGet).toHaveBeenLastCalledWith(LIST_PATH)

    const status = wrapper.find('[data-testid="admin-evidence-redaction-status"]')
    expect(status.exists()).toBe(true)
    expect(status.attributes('aria-live')).toBe('polite')
    expect(status.attributes('role')).toBeUndefined()
    expect(status.text()).toBe('Saved the rule for evidence.* / Admin.')
  })

  it('keeps the table and success message when the post-save refetch fails', async () => {
    mockGet
      .mockResolvedValueOnce({ data: [CATCH_ALL, SCOPED], error: undefined })
      .mockResolvedValueOnce({ data: undefined, error: { detail: 'refetch unavailable' } })
    const wrapper = mountView()
    await settle()

    await openCreateForm(wrapper)
    await fillRequiredFields(wrapper, 'evidence.*', 'admin')
    const classBoxes = wrapper.findAll('[data-testid="admin-evidence-redaction-field-checkbox"]')
    await classBoxes[0].setValue(true)
    await wrapper.find('form').trigger('submit')
    await settle()

    expect(mockGet).toHaveBeenCalledTimes(2)
    // Last-good content survives a failed refetch.
    expect(wrapper.find('[data-testid="admin-evidence-redaction-table"]').exists()).toBe(true)
    expect(ruleRows(wrapper)).toHaveLength(2)
    // The save's confirmation is not erased by the refetch failure.
    expect(wrapper.find('[data-testid="admin-evidence-redaction-status"]').text()).toContain(
      'Saved the rule for evidence.* / Admin.',
    )
    // The failure surfaces as a non-fatal, retry-able banner — not page-fatal.
    const refetchError = wrapper.find('[data-testid="admin-evidence-redaction-refetch-error"]')
    expect(refetchError.exists()).toBe(true)
    expect(refetchError.text()).toContain('refetch unavailable')
    expect(wrapper.find('[data-testid="admin-evidence-redaction-load-error"]').exists()).toBe(false)
  })

  it('renders the empty state when the org has no rules', async () => {
    mockRules([])
    const wrapper = mountView()
    await settle()

    expect(wrapper.find('[data-testid="admin-evidence-redaction-table"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('No redaction rules')
    expect(wrapper.find('[data-testid="admin-evidence-redaction-catch-all-missing"]').exists()).toBe(
      true,
    )
  })

  it('shows a loud warning when the mandatory catch-all row is absent', async () => {
    mockRules([SCOPED])
    const wrapper = mountView()
    await settle()

    expect(wrapper.find('[data-testid="admin-evidence-redaction-catch-all"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="admin-evidence-redaction-catch-all-missing"]').exists()).toBe(
      true,
    )
  })
})
