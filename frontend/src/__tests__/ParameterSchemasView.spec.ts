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

// Opens the editor for the first listed schema.
async function openEditor() {
  const wrapper = await mountWithSchemas()
  await wrapper.find('tbody tr').trigger('click')
  await flush()
  return wrapper
}

// Switches editor tabs by clicking the real PrimeVue tab buttons — this is
// what triggers the view's watch that lazy-loads sets/references.
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

describe('ParameterSchemasView — list', () => {
  it('renders the loading state before data arrives', async () => {
    ;(api.GET as Mock).mockReturnValue(new Promise(() => {}))
    const wrapper = mount(ParameterSchemasView)
    await nextTick()
    expect(wrapper.find('[data-testid="paramschema-new"]').exists()).toBe(true)
    expect(wrapper.find('table').exists()).toBe(false)
  })

  it('renders the schema table with name, version and parameter count', async () => {
    const wrapper = await mountWithSchemas()
    const rows = wrapper.find('tbody').findAll('tr')
    expect(rows).toHaveLength(1)
    expect(wrapper.text()).toContain('Deployment Schema')
    expect(wrapper.text()).toContain('v3')
    expect(wrapper.text()).toContain('1')
  })

  it('shows the empty state when there are no schemas (fe-003)', async () => {
    const wrapper = await mountWithSchemas([])
    expect(wrapper.find('table').exists()).toBe(false)
    expect(wrapper.text()).toContain('No parameter schemas yet')
  })

  it('surfaces the list load failure inline (fe-002 message path)', async () => {
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas') return { data: undefined, error: { detail: 'boom' } }
      return { data: { items: [] }, error: undefined }
    })
    const wrapper = mount(ParameterSchemasView)
    await flush()
    expect(wrapper.text()).toContain('boom')
  })

  it('renders the ErrorAlert retry button by default (fe-002, FAR-608 fix)', async () => {
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas') return { data: undefined, error: { detail: 'boom' } }
      return { data: { items: [] }, error: undefined }
    })
    const wrapper = mount(ParameterSchemasView)
    await flush()
    expect(wrapper.text()).toContain('boom')
    expect(wrapper.findAll('button').filter((b) => b.text() === 'Retry')).toHaveLength(1)
  })
})

describe('ParameterSchemasView — editor', () => {
  it('opens the editor with the clicked schema pre-filled', async () => {
    const wrapper = await openEditor()

    expect(wrapper.find('[data-testid="paramschema-back"]').exists()).toBe(true)
    const nameInput = wrapper.find('[data-testid="paramschema-name-input"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('Deployment Schema')
    expect(wrapper.text()).toContain('v3')
    expect(wrapper.find('[data-testid="paramschema-param-0"]').exists()).toBe(true)
    expect((wrapper.find('[data-testid="paramschema-desc-input"]').element as HTMLTextAreaElement).value).toBe('Deployment parameters')
  })

  it('back button returns to the list', async () => {
    const wrapper = await openEditor()
    await wrapper.find('[data-testid="paramschema-back"]').trigger('click')
    await flush()
    expect(wrapper.find('[data-testid="paramschema-back"]').exists()).toBe(false)
    expect(wrapper.find('table').exists()).toBe(true)
  })

  it('the editor cancel button also returns to the list', async () => {
    const wrapper = await openEditor()
    await wrapper.findAll('button').find((b) => b.text() === 'Cancel')!.trigger('click')
    await flush()
    expect(wrapper.find('table').exists()).toBe(true)
  })

  it('save button is disabled while the name is empty', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as { schemaForm: { name: string } }
    vm.schemaForm.name = ''
    await flush()
    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save as New Version')
    expect(saveBtn?.attributes('disabled')).toBeDefined()
  })

  it('saves an edited schema as a new version via PUT and shows the success banner', async () => {
    ;(api.PUT as Mock).mockResolvedValue({
      data: schemaItem({ version: 4 }),
      error: undefined,
    })
    const wrapper = await openEditor()

    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save as New Version')
    expect(saveBtn?.attributes('disabled')).toBeUndefined()
    await saveBtn!.trigger('click')
    await flush()

    expect(api.PUT).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.PUT as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/parameter-schemas/{schema_id}')
    expect(opts.params.path.schema_id).toBe('ps-1')
    expect(opts.body.version).toBe(3)
    expect(opts.body.name).toBe('Deployment Schema')
    expect(opts.body.parameters[0].name).toBe('region')
    expect(opts.body.parameters[0].default_value).toBe('us-east-1')
    expect(wrapper.text()).toContain('Schema saved as new version.')
  })

  it('shows a save error when the PUT fails', async () => {
    ;(api.PUT as Mock).mockResolvedValue({ data: undefined, error: { detail: 'name conflict' } })
    const wrapper = await openEditor()
    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save as New Version')
    await saveBtn!.trigger('click')
    await flush()
    expect(wrapper.text()).toContain('name conflict')
    expect(wrapper.text()).not.toContain('Schema saved as new version.')
  })

  it('creates a new schema via POST when no schema is being edited (new-schema path)', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: schemaItem({ id: 'ps-2', version: 1 }), error: undefined })
    const wrapper = await mountWithSchemas()
    const vm = wrapper.vm as unknown as {
      editingSchema: unknown
      schemaForm: { name: string; description: string; parameters: unknown[] }
      saveSchema: () => Promise<void>
    }
    vm.editingSchema = null
    vm.schemaForm = { name: 'Brand New', description: 'desc', parameters: [] }
    await vm.saveSchema()
    await flush()

    expect(api.POST).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.POST as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/parameter-schemas')
    expect(opts.body.name).toBe('Brand New')
    expect(opts.body.description).toBe('desc')
    expect((wrapper.vm as unknown as { saveSuccess: string }).saveSuccess).toBe('Schema created successfully.')
  })

  it('the New Schema button opens the editor with a blank form (FAR-608 fix)', async () => {
    // startNewSchema opens the editor via the creatingSchema flag instead of
    // setting editingSchema = null (which kept the list branch visible).
    const wrapper = await mountWithSchemas()
    await wrapper.find('[data-testid="paramschema-new"]').trigger('click')
    await flush()
    expect(wrapper.find('[data-testid="paramschema-back"]').exists()).toBe(true)
    expect(wrapper.find('table').exists()).toBe(false)
    const nameInput = wrapper.find('[data-testid="paramschema-name-input"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('')
    expect(api.POST).not.toHaveBeenCalled()
  })

  it('add parameter appends an editable parameter block', async () => {
    const wrapper = await openEditor()
    const addBtn = wrapper.findAll('button').find((b) => b.text().includes('Add Parameter'))
    expect(addBtn).toBeDefined()
    await addBtn!.trigger('click')
    await flush()
    expect(wrapper.find('[data-testid="paramschema-param-1"]').exists()).toBe(true)
  })

  it('remove parameter deletes the block', async () => {
    const wrapper = await openEditor()
    await wrapper.find('[data-testid="paramschema-remove-param"]').trigger('click')
    await flush()
    expect(wrapper.find('[data-testid="paramschema-param-0"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('No parameters')
  })

  it('switching a parameter type to number reveals min/max inputs and clears stale fields', async () => {
    const wrapper = await openEditor()

    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
    }
    vm.schemaForm.parameters[0].type = 'number'
    await flush()

    expect(wrapper.find('#paramschema-param-min-0').exists()).toBe(true)
    expect(wrapper.find('#paramschema-param-max-0').exists()).toBe(true)
    // onParamTypeChange cleared non-number fields
    expect(vm.schemaForm.parameters[0].options).toBeUndefined()

    vm.schemaForm.parameters[0].type = 'select'
    await flush()
    expect(wrapper.text()).toContain('Add option')

    vm.schemaForm.parameters[0].type = 'boolean'
    await flush()
    expect(vm.schemaForm.parameters[0].multiline).toBe(false)
  })

  it('a string parameter shows the multiline checkbox and a select parameter renders option editing', async () => {
    const wrapper = await openEditor()
    const vm = wrapper.vm as unknown as {
      schemaForm: { parameters: Array<Record<string, unknown>> }
    }

    // string: the parameter card renders two checkboxes — Required first,
    // then Multiline (only visible for string params).
    const checkboxes = wrapper.findAll('input[type="checkbox"]')
    expect(checkboxes).toHaveLength(2)
    await checkboxes[1].setValue(true)
    expect(vm.schemaForm.parameters[0].multiline).toBe(true)

    // select: add/remove option inputs
    vm.schemaForm.parameters[0].type = 'select'
    vm.schemaForm.parameters[0].options = ['a', 'b']
    await flush()
    const optionInputs = wrapper.findAll('input[placeholder="Option value"]')
    expect(optionInputs).toHaveLength(2)
    await wrapper.find('[data-testid="paramschema-remove-option"]').trigger('click')
    await flush()
    expect(wrapper.findAll('input[placeholder="Option value"]')).toHaveLength(1)
  })
})

describe('ParameterSchemasView — delete schema', () => {
  it('confirm box names the schema, DELETE succeeds and the list reloads', async () => {
    ;(api.DELETE as Mock).mockResolvedValue({ response: { status: 204, ok: true }, error: undefined })
    const wrapper = await mountWithSchemas()
    await wrapper.find('[data-testid="paramschema-delete"]').trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Delete "Deployment Schema"?')

    const confirmBtn = wrapper.findAll('button').find((b) => b.text() === 'Delete')
    await confirmBtn!.trigger('click')
    await flush()

    expect(api.DELETE).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.DELETE as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/parameter-schemas/{schema_id}')
    expect(opts.params.path.schema_id).toBe('ps-1')
    expect(wrapper.text()).not.toContain('Delete "Deployment Schema"?')
  })

  it('cancel closes the delete confirmation without deleting', async () => {
    const wrapper = await mountWithSchemas()
    await wrapper.find('[data-testid="paramschema-delete"]').trigger('click')
    await flush()
    await wrapper.findAll('button').find((b) => b.text() === 'Cancel')!.trigger('click')
    await flush()
    expect(wrapper.text()).not.toContain('Delete "Deployment Schema"?')
    expect(api.DELETE).not.toHaveBeenCalled()
  })

  it('409 conflict shows the referenced-by error message', async () => {
    ;(api.DELETE as Mock).mockResolvedValue({
      response: { status: 409 },
      error: { detail: 'conflict' },
    })
    const wrapper = await mountWithSchemas()
    await wrapper.find('[data-testid="paramschema-delete"]').trigger('click')
    await flush()
    const confirmBtn = wrapper.findAll('button').find((b) => b.text() === 'Delete')
    await confirmBtn!.trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Cannot delete: schema is referenced by agents or parameter sets.')
  })

  it('other delete failures surface the formatted error', async () => {
    ;(api.DELETE as Mock).mockResolvedValue({ response: { status: 500 }, error: { detail: 'db gone' } })
    const wrapper = await mountWithSchemas()
    await wrapper.find('[data-testid="paramschema-delete"]').trigger('click')
    await flush()
    await wrapper.findAll('button').find((b) => b.text() === 'Delete')!.trigger('click')
    await flush()
    expect(wrapper.text()).toContain('db gone')
    expect(wrapper.text()).not.toContain('Cannot delete: schema is referenced')
  })
})

describe('ParameterSchemasView — parameter sets tab', () => {
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

  it('loads and lists parameter sets when the tab opens', async () => {
    const wrapper = await openEditorAtSetsTab()
    expect(api.GET).toHaveBeenCalledWith(
      '/api/v1/parameter-schemas/{schema_id}/sets',
      expect.objectContaining({ params: { path: { schema_id: 'ps-1' } } }),
    )
    expect(wrapper.text()).toContain('Prod Values')
    expect(wrapper.text()).toContain('prod')
  })

  it('shows the empty message when no sets exist', async () => {
    const wrapper = await openEditorAtSetsTab([])
    expect(wrapper.text()).toContain('No parameter sets')
  })

  it('set load failure shows the error inline', async () => {
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas/{schema_id}/sets') return { data: undefined, error: { detail: 'sets down' } }
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
    expect(wrapper.text()).toContain('sets down')
  })

  it('creates a set via POST with the form values', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: setItem(), error: undefined })
    const wrapper = await openEditorAtSetsTab()
    await wrapper.find('[data-testid="paramschema-new-set"]').trigger('click')
    await flush()

    await wrapper.find('[data-testid="paramschema-set-name"]').setValue('Staging Values')
    await wrapper.find('#paramschema-set-value-region').setValue('us-west-2')
    await wrapper.find('[data-testid="paramschema-set-save"]').trigger('click')
    await flush()

    expect(api.POST).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.POST as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/parameter-schemas/{schema_id}/sets')
    expect(opts.params.path.schema_id).toBe('ps-1')
    expect(opts.body).toEqual({ name: 'Staging Values', description: null, values: { region: 'us-west-2' } })
    expect(wrapper.find('[data-testid="paramschema-set-name"]').exists()).toBe(false)
  })

  it('edit set pre-fills values and saves via PUT carrying the set version', async () => {
    ;(api.PUT as Mock).mockResolvedValue({ data: setItem({ version: 3 }), error: undefined })
    const wrapper = await openEditorAtSetsTab()
    await wrapper.find('[data-testid="paramschema-edit-set"]').trigger('click')
    await flush()

    expect((wrapper.find('[data-testid="paramschema-set-name"]').element as HTMLInputElement).value).toBe('Prod Values')
    expect((wrapper.find('#paramschema-set-value-region').element as HTMLInputElement).value).toBe('eu-west-1')

    await wrapper.find('[data-testid="paramschema-set-save"]').trigger('click')
    await flush()
    expect(api.PUT).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.PUT as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/parameter-schemas/{schema_id}/sets/{set_id}')
    expect(opts.params.path.set_id).toBe('set-1')
    expect(opts.body.version).toBe(2)
  })

  it('set save failure keeps the form open with the error text', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: undefined, error: { detail: 'dup set' } })
    const wrapper = await openEditorAtSetsTab()
    await wrapper.find('[data-testid="paramschema-new-set"]').trigger('click')
    await flush()
    await wrapper.find('[data-testid="paramschema-set-name"]').setValue('Another')
    await wrapper.find('[data-testid="paramschema-set-save"]').trigger('click')
    await flush()
    expect(wrapper.text()).toContain('dup set')
    expect(wrapper.find('[data-testid="paramschema-set-name"]').exists()).toBe(true)
  })

  it('clone set opens the form with the cloned name', async () => {
    const wrapper = await openEditorAtSetsTab()
    await wrapper.find('[data-testid="paramschema-clone-set"]').trigger('click')
    await flush()
    const name = wrapper.find('[data-testid="paramschema-set-name"]')
    expect((name.element as HTMLInputElement).value).toBe('Prod Values (clone)')
  })

  it('delete set confirm box appears and DELETE removes it', async () => {
    ;(api.DELETE as Mock).mockResolvedValue({ response: { status: 204, ok: true }, error: undefined })
    const wrapper = await openEditorAtSetsTab()
    await wrapper.find('[data-testid="paramschema-delete-set"]').trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Delete set "Prod Values"?')

    const confirmBtn = wrapper.findAll('button').find((b) => b.text() === 'Delete')
    await confirmBtn!.trigger('click')
    await flush()
    expect(api.DELETE).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.DELETE as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/parameter-schemas/{schema_id}/sets/{set_id}')
    expect(opts.params.path.set_id).toBe('set-1')
    expect(wrapper.text()).not.toContain('Delete set "Prod Values"?')
  })

  it('a failed set delete surfaces the error in the confirm block (FAR-608 fix)', async () => {
    // doDeleteSet used to only console.warn on failure; it now shows the
    // formatted error inline and keeps the confirmation open.
    ;(api.DELETE as Mock).mockResolvedValue({ response: { status: 500 }, error: { detail: 'nope' } })
    const wrapper = await openEditorAtSetsTab()
    await wrapper.find('[data-testid="paramschema-delete-set"]').trigger('click')
    await flush()
    await wrapper.findAll('button').find((b) => b.text() === 'Delete')!.trigger('click')
    await flush()
    expect(wrapper.text()).toContain('nope')
    expect(wrapper.text()).toContain('Prod Values')
  })

  it('model_backend_ref and schema_ref parameters render picker dropdowns with loaded options', async () => {
    const wrapper = await openEditorAtSetsTab([setItem({ values: {} })])
    // The value inputs live in the set editor — open it first.
    await wrapper.find('[data-testid="paramschema-new-set"]').trigger('click')
    await flush()
    ;(wrapper.vm as unknown as { schemaForm: { parameters: unknown[] } }).schemaForm.parameters = [
      { name: 'backend', label: 'Backend', type: 'model_backend_ref', required: false },
      { name: 'target', label: 'Target', type: 'schema_ref', required: false },
    ]
    await flush()
    expect(wrapper.find('#paramschema-set-value-backend').exists()).toBe(true)
    const backendOptions = wrapper.find('#paramschema-set-value-backend').findAll('option')
    expect(backendOptions.some((o) => o.text() === 'Stub Backend')).toBe(true)
    const targetOptions = wrapper.find('#paramschema-set-value-target').findAll('option')
    expect(targetOptions.some((o) => o.text() === 'Deployment Schema')).toBe(true)
  })
})

describe('ParameterSchemasView — references tab', () => {
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

  it('lists agents and sets that reference the schema', async () => {
    const wrapper = await openEditorAtRefsTab({
      agents: [{ id: 'agent-1', name: 'Deployer Agent' }],
      sets: [{ id: 'set-1', name: 'Prod Values' }],
    })
    expect(api.GET).toHaveBeenCalledWith(
      '/api/v1/parameter-schemas/{schema_id}/references',
      expect.objectContaining({ params: { path: { schema_id: 'ps-1' } } }),
    )
    expect(wrapper.text()).toContain('Agents Using This Schema')
    expect(wrapper.text()).toContain('Deployer Agent')
    expect(wrapper.text()).toContain('Prod Values')
    const agentLink = wrapper.find('a[href="/admin/agents/agent-1"]')
    expect(agentLink.exists()).toBe(true)
  })

  it('shows (0) counts when nothing references the schema', async () => {
    const wrapper = await openEditorAtRefsTab({ agents: [], sets: [] })
    expect(wrapper.text()).toContain('(0)')
  })

  it('the references error alert renders a retry button (fe-002, FAR-608 fix)', async () => {
    mockGet((url) => {
      if (url === '/api/v1/parameter-schemas/{schema_id}/references') return { data: undefined, error: { detail: 'refs down' } }
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
    expect(wrapper.text()).toContain('refs down')
    expect(wrapper.findAll('button').filter((b) => b.text() === 'Retry')).toHaveLength(1)
  })
})

describe('ParameterSchemasView — validate tab', () => {
  async function openEditorAtValidateTab() {
    const wrapper = await openEditor()
    await switchTab(wrapper, 'Validate')
    return wrapper
  }

  // The tab also renders a button labelled "Validate"; the action button is
  // the LAST match in DOM order (TabList renders before TabPanels).
  function validateButton(wrapper: Awaited<ReturnType<typeof openEditor>>) {
    const matches = wrapper.findAll('button').filter((b) => b.text() === 'Validate')
    return matches[matches.length - 1]
  }

  it('validates values via POST and shows the success banner', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: { valid: true }, error: undefined })
    const wrapper = await openEditorAtValidateTab()

    await wrapper.find('#paramschema-validate-value-region').setValue('ap-south-1')
    await validateButton(wrapper).trigger('click')
    await flush()

    expect(api.POST).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.POST as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/parameter-schemas/{schema_id}/validate')
    expect(opts.params.path.schema_id).toBe('ps-1')
    expect(opts.body).toEqual({ values: { region: 'ap-south-1' } })
    expect(wrapper.text()).toContain('Validation passed')
  })

  it('a 2xx response with valid:false is treated as a failed validation (FAR-608 fix)', async () => {
    // doValidate honours the response body's valid flag: a 200 body of
    // { valid: false, errors: [...] } renders the failed-validation state.
    ;(api.POST as Mock).mockResolvedValue({
      data: { valid: false, errors: [{ field: 'region', message: 'unknown region' }] },
      error: undefined,
    })
    const wrapper = await openEditorAtValidateTab()
    await validateButton(wrapper).trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Validation failed:')
    expect(wrapper.text()).not.toContain('Validation passed')
    expect(wrapper.text()).toContain('region')
  })

  it('an error envelope with an array detail lists the failing fields', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: undefined, error: { detail: [{ field: 'region', message: 'required' }] } })
    const wrapper = await openEditorAtValidateTab()
    await validateButton(wrapper).trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Validation failed:')
    // Each entry renders `e.field || e.message` — the field name wins.
    expect(wrapper.text()).toContain('region')
  })

  it('a thrown validation error is surfaced as a message entry', async () => {
    ;(api.POST as Mock).mockRejectedValue(new Error('validator offline'))
    const wrapper = await openEditorAtValidateTab()
    await validateButton(wrapper).trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Validation failed:')
    expect(wrapper.text()).toContain('validator offline')
  })
})

describe('ParameterSchemasView — picker preload', () => {
  it('loads model backends and schemas on mount for the ref pickers', async () => {
    await mountWithSchemas()
    const urls = (api.GET as Mock).mock.calls.map((c: unknown[]) => c[0])
    expect(urls).toContain('/api/v1/model-backends')
    expect(urls).toContain('/api/v1/schemas')
  })
})
