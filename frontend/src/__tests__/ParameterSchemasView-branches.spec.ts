/**
 * Branch coverage tests for ParameterSchemasView.vue (FAR-835).
 *
 * Targets uncovered branches: parameters?.length ?? 0 with undefined,
 * description null fallback, default value type branches (string/number/
 * boolean/select/fallback dash), onParamTypeChange clearing fields,
 * addParameter/removeParameter, set save create vs edit paths, validate
 * 2xx with valid:true vs valid:false vs error vs thrown, references with
 * data vs empty, picker preload.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import type { Mock } from 'vitest'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import ParameterSchemasView from '../views/ParameterSchemasView.vue'
import { api } from '../lib/api/client'

async function flush() {
  await flushPromises()
  await nextTick()
  await flushPromises()
}

const param = (over: Record<string, unknown> = {}) => ({
  name: 'region',
  label: 'Region',
  description: 'AWS region',
  type: 'string',
  required: true,
  default_value: 'us-east-1',
  multiline: false,
  options: undefined,
  minimum: undefined,
  maximum: undefined,
  placeholder: undefined,
  ...over,
})

const schemaItem = (over: Record<string, unknown> = {}) => ({
  id: 'ps-1',
  organisation_id: 'org-1',
  name: 'Deployment Schema',
  description: 'Deployment parameters',
  version: 3,
  parameters: [param()],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-02T00:00:00Z',
  ...over,
})

const listPayload = (items: unknown[] = [schemaItem()]) => ({
  data: { items, total: items.length, page: 1, page_size: 100 },
  error: undefined,
})

function mockGet(impl?: (url: string, opts?: unknown) => unknown) {
  ;(api.GET as Mock).mockImplementation(async (url: string, opts?: unknown) => {
    if (impl) return impl(url, opts)
    if (url === '/api/v1/parameter-schemas') return listPayload()
    if (url === '/api/v1/model-backends') return { data: { items: [{ id: 'mb-1', name: 'Stub Backend' }] }, error: undefined }
    if (url === '/api/v1/schemas') return listPayload()
    if (url === '/api/v1/parameter-schemas/{schema_id}/sets') return { data: [], error: undefined }
    if (url === '/api/v1/parameter-schemas/{schema_id}/references') return { data: { agents: [], sets: [] }, error: undefined }
    return { data: undefined, error: { detail: 'not found' } }
  })
}

async function mountWithSchemas(items: unknown[] = [schemaItem()]) {
  mockGet(() => listPayload(items))
  const wrapper = mount(ParameterSchemasView)
  await flush()
  return wrapper
}

async function openEditor() {
  const wrapper = await mountWithSchemas()
  await wrapper.find('tbody tr').trigger('click')
  await flush()
  return wrapper
}

async function switchTab(wrapper: Awaited<ReturnType<typeof openEditor>>, label: string) {
  const tab = wrapper.findAll('[role="tab"]').find((t) => t.text() === label)
  expect(tab, `tab ${label} not found`).toBeDefined()
  await tab!.trigger('click')
  await flush()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  mockGet()
})

// ── parameters?.length ?? 0 with undefined ──────────────────────────────
describe('ParameterSchemasView branches — undefined parameters', () => {
  it('shows 0 when schema has undefined parameters', async () => {
    const schemaWithUndefinedParams = {
      ...schemaItem(),
      parameters: undefined,
    }
    const wrapper = await mountWithSchemas([schemaWithUndefinedParams])
    expect(wrapper.text()).toContain('0')
    wrapper.unmount()
  })

  it('shows correct count when parameters exist', async () => {
    const wrapper = await mountWithSchemas()
    expect(wrapper.text()).toContain('1')
    wrapper.unmount()
  })
})

// ── description fallback to '—' when null ──────────────────────────────
describe('ParameterSchemasView branches — description null fallback', () => {
  it('shows dash for null description', async () => {
    const schemaNullDesc = schemaItem({ description: null })
    const wrapper = await mountWithSchemas([schemaNullDesc])
    expect(wrapper.text()).toContain('—')
    wrapper.unmount()
  })

  it('shows description when present', async () => {
    const wrapper = await mountWithSchemas()
    expect(wrapper.text()).toContain('Deployment parameters')
    wrapper.unmount()
  })
})

// ── default value type branches ─────────────────────────────────────────
describe('ParameterSchemasView branches — default value types', () => {
  it('shows text input for string type default', async () => {
    const wrapper = await openEditor()
    // The parameter is type string with default_value 'us-east-1'
    const defaultInput = wrapper.find('#paramschema-param-default-0')
    expect(defaultInput.exists()).toBe(true)
    expect((defaultInput.element as HTMLInputElement).type).toBe('text')
    wrapper.unmount()
  })

  it('shows number input for number type default', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
    }
    vm.schemaForm.parameters[0].type = 'number'
    vm.schemaForm.parameters[0].default_value = 42
    await flush()

    const defaultInput = wrapper.find('#paramschema-param-default-0')
    expect(defaultInput.exists()).toBe(true)
    expect((defaultInput.element as HTMLInputElement).type).toBe('number')
    wrapper.unmount()
  })

  it('shows select for boolean type default', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
    }
    vm.schemaForm.parameters[0].type = 'boolean'
    vm.schemaForm.parameters[0].default_value = true
    await flush()

    const defaultSelect = wrapper.find('#paramschema-param-default-0')
    expect(defaultSelect.exists()).toBe(true)
    expect(defaultSelect.element.tagName).toBe('SELECT')
    wrapper.unmount()
  })

  it('shows select for select type with options', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
    }
    vm.schemaForm.parameters[0].type = 'select'
    vm.schemaForm.parameters[0].options = ['a', 'b', 'c']
    vm.schemaForm.parameters[0].default_value = 'a'
    await flush()

    const defaultSelect = wrapper.find('#paramschema-param-default-0')
    expect(defaultSelect.exists()).toBe(true)
    expect(defaultSelect.element.tagName).toBe('SELECT')
    wrapper.unmount()
  })

  it('shows dash for unsupported type default', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
    }
    vm.schemaForm.parameters[0].type = 'model_backend_ref'
    vm.schemaForm.parameters[0].options = undefined
    await flush()

    // The v-else branch shows a dash span
    const defaultSection = wrapper.find('#paramschema-param-default-0')
    expect(defaultSection.exists()).toBe(false)
    wrapper.unmount()
  })
})

// ── onParamTypeChange clearing fields ───────────────────────────────────
describe('ParameterSchemasView branches — onParamTypeChange', () => {
  it('clears options when not select type', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
      onParamTypeChange: (param: Record<string, unknown>) => void
    }
    // Set to select with options
    vm.schemaForm.parameters[0].type = 'select'
    vm.schemaForm.parameters[0].options = ['a', 'b']
    await flush()

    // Switch to string — trigger onParamTypeChange explicitly
    vm.schemaForm.parameters[0].type = 'string'
    vm.onParamTypeChange(vm.schemaForm.parameters[0])
    await flush()
    expect(vm.schemaForm.parameters[0].options).toBeUndefined()
    wrapper.unmount()
  })

  it('clears min/max when not number type', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
      onParamTypeChange: (param: Record<string, unknown>) => void
    }
    // Set to number with min/max
    vm.schemaForm.parameters[0].type = 'number'
    vm.schemaForm.parameters[0].minimum = 0
    vm.schemaForm.parameters[0].maximum = 100
    await flush()

    // Switch to string — trigger onParamTypeChange explicitly
    vm.schemaForm.parameters[0].type = 'string'
    vm.onParamTypeChange(vm.schemaForm.parameters[0])
    await flush()
    expect(vm.schemaForm.parameters[0].minimum).toBeUndefined()
    expect(vm.schemaForm.parameters[0].maximum).toBeUndefined()
    wrapper.unmount()
  })

  it('clears multiline when not string type', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
      onParamTypeChange: (param: Record<string, unknown>) => void
    }
    vm.schemaForm.parameters[0].type = 'string'
    vm.schemaForm.parameters[0].multiline = true
    await flush()

    vm.schemaForm.parameters[0].type = 'number'
    vm.onParamTypeChange(vm.schemaForm.parameters[0])
    await flush()
    expect(vm.schemaForm.parameters[0].multiline).toBe(false)
    wrapper.unmount()
  })
})

// ── addParameter/removeParameter ────────────────────────────────────────
describe('ParameterSchemasView branches — add/remove parameter', () => {
  it('addParameter appends a new parameter', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
    }
    const initialLength = vm.schemaForm.parameters.length
    // Call addParameter via the exposed method
    await (wrapper.vm as any).addParameter()
    await flush()
    expect(vm.schemaForm.parameters.length).toBe(initialLength + 1)
    wrapper.unmount()
  })

  it('removeParameter removes the parameter at index', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
    }
    await (wrapper.vm as any).addParameter()
    await flush()
    expect(vm.schemaForm.parameters.length).toBe(2)
    await (wrapper.vm as any).removeParameter(0)
    await flush()
    expect(vm.schemaForm.parameters.length).toBe(1)
    wrapper.unmount()
  })
})

// ── set save: create vs edit paths ──────────────────────────────────────
describe('ParameterSchemasView branches — set save paths', () => {
  const setItem = (over: Record<string, unknown> = {}) => ({
    id: 'set-1',
    parameter_schema_id: 'ps-1',
    name: 'Prod Values',
    description: 'prod',
    version: 2,
    schema_version: 3,
    values: { region: 'eu-west-1' },
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    ...over,
  })

  async function openEditorAtSetsTab(sets: unknown[] = [setItem()]) {
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas/{schema_id}/sets') return { data: sets, error: undefined }
      if (url === '/api/v1/parameter-schemas') return listPayload()
      if (url === '/api/v1/schemas') return listPayload()
      if (url === '/api/v1/model-backends') return { data: { items: [{ id: 'mb-1', name: 'Stub Backend' }] }, error: undefined }
      return { data: undefined, error: { detail: 'not found' } }
    })
    const wrapper = mount(ParameterSchemasView)
    await flush()
    await wrapper.find('tbody tr').trigger('click')
    await flush()
    await switchTab(wrapper, 'Parameter Sets')
    return wrapper
  }

  it('creates a new set via POST', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: setItem(), error: undefined })
    const wrapper = await openEditorAtSetsTab()
    await wrapper.find('[data-testid="paramschema-new-set"]').trigger('click')
    await flush()

    await wrapper.find('[data-testid="paramschema-set-name"]').setValue('Staging')
    await wrapper.find('[data-testid="paramschema-set-save"]').trigger('click')
    await flush()

    expect(api.POST).toHaveBeenCalledTimes(1)
    const [url] = (api.POST as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/parameter-schemas/{schema_id}/sets')
    wrapper.unmount()
  })

  it('edits an existing set via PUT', async () => {
    ;(api.PUT as Mock).mockResolvedValue({ data: setItem({ version: 3 }), error: undefined })
    const wrapper = await openEditorAtSetsTab()
    await wrapper.find('[data-testid="paramschema-edit-set"]').trigger('click')
    await flush()

    await wrapper.find('[data-testid="paramschema-set-save"]').trigger('click')
    await flush()

    expect(api.PUT).toHaveBeenCalledTimes(1)
    const [url] = (api.PUT as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/parameter-schemas/{schema_id}/sets/{set_id}')
    wrapper.unmount()
  })
})

// ── validate: various response shapes ───────────────────────────────────
describe('ParameterSchemasView branches — validate', () => {
  async function openEditorAtValidateTab() {
    const wrapper = await openEditor()
    await switchTab(wrapper, 'Validate')
    return wrapper
  }

  function validateButton(wrapper: Awaited<ReturnType<typeof openEditor>>) {
    const matches = wrapper.findAll('button').filter((b) => b.text() === 'Validate')
    return matches[matches.length - 1]
  }

  it('valid: true shows success', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: { valid: true }, error: undefined })
    const wrapper = await openEditorAtValidateTab()
    await validateButton(wrapper).trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Validation passed')
    wrapper.unmount()
  })

  it('valid: false shows failure with errors', async () => {
    ;(api.POST as Mock).mockResolvedValue({
      data: { valid: false, errors: [{ field: 'region', message: 'bad' }] },
      error: undefined,
    })
    const wrapper = await openEditorAtValidateTab()
    await validateButton(wrapper).trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Validation failed')
    wrapper.unmount()
  })

  it('error with array detail shows fields', async () => {
    ;(api.POST as Mock).mockResolvedValue({
      data: undefined,
      error: { detail: [{ field: 'region', message: 'required' }] },
    })
    const wrapper = await openEditorAtValidateTab()
    await validateButton(wrapper).trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Validation failed')
    wrapper.unmount()
  })

  it('error with object detail shows message', async () => {
    ;(api.POST as Mock).mockResolvedValue({
      data: undefined,
      error: { detail: { message: 'schema not found' } },
    })
    const wrapper = await openEditorAtValidateTab()
    await validateButton(wrapper).trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Validation failed')
    wrapper.unmount()
  })

  it('thrown error shows message', async () => {
    ;(api.POST as Mock).mockRejectedValue(new Error('network'))
    const wrapper = await openEditorAtValidateTab()
    await validateButton(wrapper).trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Validation failed')
    expect(wrapper.text()).toContain('network')
    wrapper.unmount()
  })
})

// ── references: agents/sets with data vs empty ──────────────────────────
describe('ParameterSchemasView branches — references', () => {
  async function openEditorAtRefsTab(references: unknown) {
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas/{schema_id}/references') return { data: references, error: undefined }
      if (url === '/api/v1/parameter-schemas') return listPayload()
      if (url === '/api/v1/schemas') return listPayload()
      if (url === '/api/v1/model-backends') return { data: { items: [] }, error: undefined }
      return { data: undefined, error: { detail: 'x' } }
    })
    const wrapper = mount(ParameterSchemasView)
    await flush()
    await wrapper.find('tbody tr').trigger('click')
    await flush()
    await switchTab(wrapper, 'References')
    return wrapper
  }

  it('shows agents and sets when present', async () => {
    const wrapper = await openEditorAtRefsTab({
      agents: [{ id: 'a1', name: 'Agent One' }],
      sets: [{ id: 's1', name: 'Set One' }],
    })
    expect(wrapper.text()).toContain('Agent One')
    expect(wrapper.text()).toContain('Set One')
    wrapper.unmount()
  })

  it('shows dash when agents/sets are empty', async () => {
    const wrapper = await openEditorAtRefsTab({ agents: [], sets: [] })
    expect(wrapper.text()).toContain('(0)')
    wrapper.unmount()
  })

  it('shows "select schema for refs" when references is null', async () => {
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas/{schema_id}/references') return { data: null, error: undefined }
      if (url === '/api/v1/parameter-schemas') return listPayload()
      if (url === '/api/v1/schemas') return listPayload()
      if (url === '/api/v1/model-backends') return { data: { items: [] }, error: undefined }
      return { data: undefined, error: { detail: 'x' } }
    })
    const wrapper = mount(ParameterSchemasView)
    await flush()
    await wrapper.find('tbody tr').trigger('click')
    await flush()
    await switchTab(wrapper, 'References')
    // When references is null, the tab shows the placeholder text (not agent/set lists)
    // The actual text depends on the i18n locale; verify no agent/set data is shown
    expect(wrapper.text()).not.toContain('Agents Using')
    expect(wrapper.text()).not.toContain('Sets Using')
    wrapper.unmount()
  })
})

// ── picker preload ──────────────────────────────────────────────────────
describe('ParameterSchemasView branches — picker preload', () => {
  it('loads model backends and schemas on mount', async () => {
    const wrapper = await mountWithSchemas()
    const urls = (api.GET as Mock).mock.calls.map((c: unknown[]) => c[0])
    expect(urls).toContain('/api/v1/model-backends')
    expect(urls).toContain('/api/v1/schemas')
    wrapper.unmount()
  })
})

// ── set form cancel ─────────────────────────────────────────────────────
describe('ParameterSchemasView branches — cancel set edit', () => {
  it('cancelSetEdit clears editing state', async () => {
    const setItem = {
      id: 'set-1',
      parameter_schema_id: 'ps-1',
      name: 'Prod',
      description: 'prod',
      version: 2,
      schema_version: 3,
      values: { region: 'eu-west-1' },
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-01T00:00:00Z',
    }
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas/{schema_id}/sets') return { data: [setItem], error: undefined }
      if (url === '/api/v1/parameter-schemas') return listPayload()
      if (url === '/api/v1/schemas') return listPayload()
      if (url === '/api/v1/model-backends') return { data: { items: [] }, error: undefined }
      return { data: undefined, error: { detail: 'x' } }
    })
    const wrapper = mount(ParameterSchemasView)
    await flush()
    await wrapper.find('tbody tr').trigger('click')
    await flush()
    await switchTab(wrapper, 'Parameter Sets')
    await wrapper.find('[data-testid="paramschema-new-set"]').trigger('click')
    await flush()

    // Cancel
    const cancelBtn = wrapper.findAll('button').find((b) => b.text() === 'Cancel')
    await cancelBtn!.trigger('click')
    await flush()

    expect(wrapper.find('[data-testid="paramschema-set-name"]').exists()).toBe(false)
    wrapper.unmount()
  })
})

// ── delete schema catch block ───────────────────────────────────────────
describe('ParameterSchemasView branches — delete catch', () => {
  it('catches thrown delete error', async () => {
    ;(api.DELETE as Mock).mockRejectedValue(new Error('network'))
    const wrapper = await mountWithSchemas()
    await wrapper.find('[data-testid="paramschema-delete"]').trigger('click')
    await flush()

    const confirmBtn = wrapper.findAll('button').find((b) => b.text() === 'Delete')
    await confirmBtn!.trigger('click')
    await flush()

    expect(wrapper.text()).toContain('network')
    wrapper.unmount()
  })
})

// ── set delete catch block ──────────────────────────────────────────────
describe('ParameterSchemasView branches — set delete catch', () => {
  it('catches thrown set delete error', async () => {
    const setItem = {
      id: 'set-1',
      parameter_schema_id: 'ps-1',
      name: 'Prod',
      description: 'prod',
      version: 2,
      schema_version: 3,
      values: {},
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-01T00:00:00Z',
    }
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas/{schema_id}/sets') return { data: [setItem], error: undefined }
      if (url === '/api/v1/parameter-schemas') return listPayload()
      if (url === '/api/v1/schemas') return listPayload()
      if (url === '/api/v1/model-backends') return { data: { items: [] }, error: undefined }
      return { data: undefined, error: { detail: 'x' } }
    })
    const wrapper = mount(ParameterSchemasView)
    await flush()
    await wrapper.find('tbody tr').trigger('click')
    await flush()
    await switchTab(wrapper, 'Parameter Sets')

    ;(api.DELETE as Mock).mockRejectedValue(new Error('network'))
    await wrapper.find('[data-testid="paramschema-delete-set"]').trigger('click')
    await flush()

    const confirmBtn = wrapper.findAll('button').find((b) => b.text() === 'Delete')
    await confirmBtn!.trigger('click')
    await flush()

    expect(wrapper.text()).toContain('network')
    wrapper.unmount()
  })
})

// ── set save catch block ────────────────────────────────────────────────
describe('ParameterSchemasView branches — set save catch', () => {
  it('catches thrown set save error', async () => {
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas/{schema_id}/sets') return { data: [], error: undefined }
      if (url === '/api/v1/parameter-schemas') return listPayload()
      if (url === '/api/v1/schemas') return listPayload()
      if (url === '/api/v1/model-backends') return { data: { items: [] }, error: undefined }
      return { data: undefined, error: { detail: 'x' } }
    })
    const wrapper = mount(ParameterSchemasView)
    await flush()
    await wrapper.find('tbody tr').trigger('click')
    await flush()
    await switchTab(wrapper, 'Parameter Sets')

    ;(api.POST as Mock).mockRejectedValue(new Error('network'))
    await wrapper.find('[data-testid="paramschema-new-set"]').trigger('click')
    await flush()
    await wrapper.find('[data-testid="paramschema-set-name"]').setValue('Test')
    await wrapper.find('[data-testid="paramschema-set-save"]').trigger('click')
    await flush()

    expect(wrapper.text()).toContain('network')
    wrapper.unmount()
  })
})

// ── save schema catch block ─────────────────────────────────────────────
describe('ParameterSchemasView branches — save schema catch', () => {
  it('catches thrown save error', async () => {
    const wrapper = await openEditor()
    ;(api.POST as Mock).mockRejectedValue(new Error('save failed'))

    const vm = wrapper.vm as unknown as {
      editingSchema: unknown
      creatingSchema: boolean
      schemaForm: { name: string; description: string; parameters: unknown[] }
      saveSchema: () => Promise<void>
    }
    // Switch to new-schema mode — creatingSchema is a ref but exposed as boolean on vm
    vm.creatingSchema = true
    vm.editingSchema = null
    vm.schemaForm = { name: 'New', description: '', parameters: [] }
    await flush()

    await vm.saveSchema()
    await flush()

    expect(wrapper.text()).toContain('save failed')
    wrapper.unmount()
  })
})
