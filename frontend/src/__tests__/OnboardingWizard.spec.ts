import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick as vueNextTick } from 'vue'
import type { Mock } from 'vitest'

async function nextTick() { await vueNextTick(); await flushPromises() }

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import OnboardingWizard from '../views/OnboardingWizard.vue'
import { api } from '../lib/api/client'

const connector = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  id,
  name,
  connector_type_id: 'github',
  status: 'active',
  config_json: {},
  ...over,
})

const inferResponse = {
  suggestion_name: 'GitHub Issues',
  suggestion_description: 'Fields for issues',
  definition_json: {
    type: 'object',
    properties: {
      title: { type: 'string', description: 'Issue title' },
      points: { type: 'number' },
    },
    required: ['title'],
  },
}

const libraryItem = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  primitive_type: 'agent',
  name: `Item ${id}`,
  description: `Description ${id}`,
  tags: ['deploy', 'ci'],
  visibility: 'org',
  ...over,
})

function mockApis(over: {
  connectors?: unknown
  connectorsError?: boolean
  library?: unknown
  libraryError?: boolean
} = {}) {
  ;(api.GET as Mock).mockImplementation(async (url: string) => {
    if (url === '/api/v1/connectors') {
      if (over.connectorsError) throw new Error('connectors offline')
      return { data: { items: over.connectors ?? [connector('conn-1', 'My GitHub')] }, error: undefined }
    }
    if (url === '/api/v1/libraries') {
      if (over.libraryError) return { data: undefined, error: { detail: 'library offline' } }
      return { data: { items: over.library ?? [libraryItem('lib-1'), libraryItem('lib-2', { primitive_type: 'pipeline_template', tags: [] })] }, error: undefined }
    }
    return { data: { items: [] }, error: undefined }
  })
  ;(api.POST as Mock).mockImplementation(async (url: string) => {
    if (url === '/api/v1/schemas/infer') return { data: inferResponse, error: undefined }
    if (url === '/api/v1/schemas') return { data: { id: 'schema-1' }, error: undefined }
    if (url === '/api/v1/schemas/{schema_id}/versions') return { data: { id: 'ver-1' }, error: undefined }
    if (url === '/api/v1/pipelines') return { data: { id: 'pipe-1', name: 'My Pipeline' }, error: undefined }
    if (url === '/api/v1/runs') return { data: { id: 'run-1' }, error: undefined }
    return { data: null, error: undefined }
  })
}

function mountWizard() {
  return mount(OnboardingWizard)
}

async function clickNext(wrapper: ReturnType<typeof mountWizard>) {
  await wrapper.find('[data-testid="onboarding-wizard-next"]').trigger('click')
  await nextTick()
}

async function clickPrevious(wrapper: ReturnType<typeof mountWizard>) {
  await wrapper.find('[data-testid="onboarding-wizard-previous"]').trigger('click')
  await nextTick()
}

// Drives the wizard to step 2 (Run Inference): a connector must be selected.
async function advanceToStep2() {
  const wrapper = mountWizard()
  await nextTick()
  await clickNext(wrapper)
  await wrapper.find('[data-testid="onboarding-wizard-connector-card"]').trigger('click')
  await nextTick()
  await clickNext(wrapper)
  return wrapper
}

// Drives the wizard to step 3 (Review Schemas): inference must have run.
async function advanceToStep3() {
  const wrapper = await advanceToStep2()
  await wrapper.find('[data-testid="onboarding-wizard-resource-type"]').setValue('issues')
  await wrapper.find('[data-testid="onboarding-wizard-infer-schema"]').trigger('click')
  await nextTick()
  await clickNext(wrapper)
  return wrapper
}

// Drives the wizard to step 4 (Browse Library): the schema must be saved.
async function advanceToStep4() {
  const wrapper = await advanceToStep3()
  await wrapper.find('[data-testid="onboarding-wizard-confirm-save-schema"]').trigger('click')
  await nextTick()
  await clickNext(wrapper)
  return wrapper
}

// Drives the wizard to step 5 (Wire Pipeline), saving the schema on the way.
async function advanceToStep5() {
  const wrapper = await advanceToStep4()
  await clickNext(wrapper) // -> step 5
  return wrapper
}

// Drives the wizard to step 6 (Done) by creating the pipeline.
async function advanceToDone() {
  const wrapper = await advanceToStep5()
  await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
  await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
  await nextTick()
  await clickNext(wrapper) // Finish
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApis()
})

describe('OnboardingWizard — navigation & gating', () => {
  it('renders the welcome step with the 6-step guide', async () => {
    const wrapper = mountWizard()
    await nextTick()
    expect(wrapper.text()).toContain('SDLC Onboarding')
    expect(wrapper.text()).toContain('Welcome')
    expect(wrapper.text()).toContain('Connect Tools:')
    expect(wrapper.text()).toContain('Wire Pipeline:')
    expect(wrapper.find('[data-testid="onboarding-wizard-previous"]').exists()).toBe(false)
  })

  it('loads connectors as soon as the wizard mounts', async () => {
    mountWizard()
    await nextTick()
    expect(api.GET).toHaveBeenCalledWith('/api/v1/connectors')
  })

  it('step 1 disables Next until a connector is selected, then proceeds', async () => {
    const wrapper = mountWizard()
    await nextTick()
    await clickNext(wrapper)

    expect(wrapper.text()).toContain('Connect Tools')
    expect(wrapper.text()).toContain('My GitHub')
    expect(wrapper.find('[data-testid="onboarding-wizard-next"]').attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="onboarding-wizard-connector-card"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-next"]').attributes('disabled')).toBeUndefined()
  })

  it('step 1 shows the empty state with a create-connector link when none exist', async () => {
    mockApis({ connectors: [] })
    const wrapper = mountWizard()
    await nextTick()
    await clickNext(wrapper)
    expect(wrapper.text()).toContain('No connectors found.')
    expect(wrapper.find('[data-testid="onboarding-wizard-create-connector"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="onboarding-wizard-connector-card"]').exists()).toBe(false)
  })

  it('step 1 shows the connectors load failure', async () => {
    mockApis({ connectorsError: true })
    const wrapper = mountWizard()
    await nextTick()
    await clickNext(wrapper)
    expect(wrapper.text()).toContain('Failed to load connectors:')
    expect(wrapper.text()).toContain('connectors offline')
  })

  it('previous returns to the prior step and skip-to-end jumps to Done', async () => {
    const wrapper = await advanceToStep2()
    await clickPrevious(wrapper)
    expect(wrapper.text()).toContain('Connect Tools')

    await clickNext(wrapper) // back to step 2
    await wrapper.find('[data-testid="onboarding-wizard-skip-to-end"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain("You're all set!")
  })

  it('step 3 shows the no-schema message when reached without a draft', async () => {
    const wrapper = mountWizard()
    await nextTick()
    const vm = wrapper.vm as unknown as { currentStep: number }
    vm.currentStep = 3
    await nextTick()
    expect(wrapper.text()).toContain('No schema inferred yet.')
  })
})

describe('OnboardingWizard — inference (step 2)', () => {
  it('disables Infer until a resource type is entered, then POSTs and renders the draft fields', async () => {
    const wrapper = await advanceToStep2()

    const inferBtn = wrapper.find('[data-testid="onboarding-wizard-infer-schema"]')
    expect(inferBtn.attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="onboarding-wizard-resource-type"]').setValue(' issues ')
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-infer-schema"]').attributes('disabled')).toBeUndefined()

    await wrapper.find('[data-testid="onboarding-wizard-infer-schema"]').trigger('click')
    await nextTick()

    expect(api.POST).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.POST as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/schemas/infer')
    expect(opts.body).toEqual({
      connector_instance_id: 'conn-1',
      sample_query: { resource: 'issues', filters: {}, limit: 200 },
    })

    expect(wrapper.text()).toContain('Draft: GitHub Issues')
    expect(wrapper.text()).toContain('title')
    expect(wrapper.text()).toContain('Issue title')
    expect(wrapper.text()).toContain('points')
    // required flag rendering: title yes, points no (locale renders lowercase)
    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('yes')
    expect(rows[1].text()).toContain('no')
  })

  it('shows the inference failure from the error envelope', async () => {
    ;(api.POST as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/schemas/infer') return { data: undefined, error: { detail: 'inference blew up' } }
      return { data: null, error: undefined }
    })
    const wrapper = await advanceToStep2()
    await wrapper.find('[data-testid="onboarding-wizard-resource-type"]').setValue('issues')
    await wrapper.find('[data-testid="onboarding-wizard-infer-schema"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Schema inference failed:')
    expect(wrapper.text()).toContain('inference blew up')
    // Next stays disabled without a draft schema.
    expect(wrapper.find('[data-testid="onboarding-wizard-next"]').attributes('disabled')).toBeDefined()
  })
})

describe('OnboardingWizard — save schema (step 3)', () => {
  it('saves the schema and publishes v1, then gates on the published id', async () => {
    const wrapper = await advanceToStep3()

    const nameInput = wrapper.find('[data-testid="onboarding-wizard-schema-name"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('GitHub Issues')
    await nameInput.setValue('Renamed Schema')
    await wrapper.find('[data-testid="onboarding-wizard-schema-description"]').setValue('Updated description')

    await wrapper.find('[data-testid="onboarding-wizard-confirm-save-schema"]').trigger('click')
    await nextTick()

    // calls[0] is the inference POST; then schema create + version publish.
    expect(api.POST).toHaveBeenCalledTimes(3)
    const [, schemaCall] = (api.POST as Mock).mock.calls[1]
    expect(schemaCall.body).toEqual({ name: 'Renamed Schema', description: 'Updated description' })
    const [versionUrl, versionOpts] = (api.POST as Mock).mock.calls[2]
    expect(versionUrl).toBe('/api/v1/schemas/{schema_id}/versions')
    expect(versionOpts.params.path.schema_id).toBe('schema-1')
    expect(versionOpts.body).toMatchObject({ version: 'v1', version_number: 1, published: true })
    expect(versionOpts.body.definition_json).toEqual(inferResponse.definition_json)

    expect(wrapper.text()).toContain('Schema "Renamed Schema" saved.')
    expect(wrapper.find('[data-testid="onboarding-wizard-next"]').attributes('disabled')).toBeUndefined()
  })

  it('shows the save failure when schema creation returns an error envelope', async () => {
    ;(api.POST as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/schemas/infer') return { data: inferResponse, error: undefined }
      if (url === '/api/v1/schemas') return { data: undefined, error: { detail: 'duplicate schema name' } }
      return { data: null, error: undefined }
    })
    const wrapper = await advanceToStep3()
    await wrapper.find('[data-testid="onboarding-wizard-confirm-save-schema"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Save failed:')
    expect(wrapper.text()).toContain('duplicate schema name')
    expect(wrapper.find('[data-testid="onboarding-wizard-next"]').attributes('disabled')).toBeDefined()
  })

  it('shows the save failure when the version publish fails', async () => {
    ;(api.POST as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/schemas/infer') return { data: inferResponse, error: undefined }
      if (url === '/api/v1/schemas/{schema_id}/versions') return { data: undefined, error: { detail: 'version rejected' } }
      if (url === '/api/v1/schemas') return { data: { id: 'schema-1' }, error: undefined }
      return { data: null, error: undefined }
    })
    const wrapper = await advanceToStep3()
    await wrapper.find('[data-testid="onboarding-wizard-confirm-save-schema"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Save failed:')
    expect(wrapper.text()).toContain('version rejected')
  })

  it('shows the no-response failure when schema creation returns nothing', async () => {
    ;(api.POST as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/schemas/infer') return { data: inferResponse, error: undefined }
      if (url === '/api/v1/schemas') return { data: undefined, error: undefined }
      return { data: null, error: undefined }
    })
    const wrapper = await advanceToStep3()
    await wrapper.find('[data-testid="onboarding-wizard-confirm-save-schema"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Save failed: no response')
  })
})

describe('OnboardingWizard — library (step 4)', () => {
  it('loads the library when the step opens and renders items', async () => {
    const wrapper = await advanceToStep4()
    await nextTick()

    expect(api.GET).toHaveBeenCalledWith('/api/v1/libraries', { params: { query: { page: 1, page_size: 50 } } })
    const items = wrapper.findAll('[data-testid="onboarding-wizard-library-item"]')
    expect(items).toHaveLength(2)
    expect(wrapper.text()).toContain('Item lib-1')
    expect(wrapper.text()).toContain('deploy')
  })

  it('filters items by the search box and by type', async () => {
    const wrapper = await advanceToStep4()

    await wrapper.find('[data-testid="onboarding-wizard-library-search"]').setValue('Item lib-2')
    await nextTick()
    expect(wrapper.findAll('[data-testid="onboarding-wizard-library-item"]')).toHaveLength(1)

    await wrapper.find('[data-testid="onboarding-wizard-library-search"]').setValue('')
    const vm = wrapper.vm as unknown as { libraryTypeFilter: string }
    vm.libraryTypeFilter = 'agent'
    await nextTick()
    const filtered = wrapper.findAll('[data-testid="onboarding-wizard-library-item"]')
    expect(filtered).toHaveLength(1)
    expect(filtered[0].text()).toContain('Item lib-1')
  })

  it('selecting an item highlights it and selecting again deselects', async () => {
    const wrapper = await advanceToStep4()

    const first = wrapper.findAll('[data-testid="onboarding-wizard-library-item"]')[0]
    await first.trigger('click')
    await nextTick()
    expect(first.classes()).toContain('border-primary')

    await first.trigger('click')
    await nextTick()
    expect(first.classes()).not.toContain('border-primary')
  })

  it('the library error surfaces the error detail (FAR-608 fix)', async () => {
    // loadLibrary formats the error envelope with formatApiError, so the
    // detail accompanies the "Failed to load library" message.
    mockApis({ libraryError: true })
    const wrapper = await advanceToStep4()
    await nextTick()
    expect(wrapper.text()).toContain('Failed to load library')
    expect(wrapper.text()).toContain('library offline')
  })

  it('shows the empty library state', async () => {
    mockApis({ library: [] })
    const wrapper = await advanceToStep4()
    await nextTick()
    expect(wrapper.text()).toContain('No library items available.')
  })
})

describe('OnboardingWizard — create pipeline (step 5)', () => {
  it('disables Create without a name, then POSTs the pipeline with defaults', async () => {
    const wrapper = await advanceToStep5()

    expect(wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('  My Pipeline  ')
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-description"]').setValue('Does things')
    await nextTick()
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()

    expect(api.POST).toHaveBeenCalledTimes(4)
    const [url, opts] = (api.POST as Mock).mock.calls[3]
    expect(url).toBe('/api/v1/pipelines')
    expect(opts.body).toMatchObject({
      name: 'My Pipeline',
      description: 'Does things',
      visibility: 'org',
      default_autonomy_level: 'balanced',
    })

    // Finish (Next) becomes available after creation.
    const nextBtn = wrapper.find('[data-testid="onboarding-wizard-next"]')
    expect(nextBtn.attributes('disabled')).toBeUndefined()
    expect(nextBtn.text()).toContain('Finish')
  })

  it('the pipeline creation failure surfaces the error detail (FAR-608 fix)', async () => {
    // createPipeline formats the error envelope with formatApiError, so
    // "quota exhausted" accompanies the "Failed to create pipeline" message.
    ;(api.POST as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') return { data: undefined, error: { detail: 'quota exhausted' } }
      if (url === '/api/v1/schemas/infer') return { data: inferResponse, error: undefined }
      if (url === '/api/v1/schemas') return { data: { id: 'schema-1' }, error: undefined }
      if (url === '/api/v1/schemas/{schema_id}/versions') return { data: { id: 'ver-1' }, error: undefined }
      return { data: null, error: undefined }
    })
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('Nope')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Failed to create pipeline')
    expect(wrapper.text()).toContain('quota exhausted')
    expect(wrapper.find('[data-testid="onboarding-wizard-next"]').attributes('disabled')).toBeDefined()
  })

  it('summarises the selected library item', async () => {
    const wrapper = await advanceToStep4()
    await nextTick()
    await wrapper.findAll('[data-testid="onboarding-wizard-library-item"]')[0].trigger('click')
    await nextTick()
    await clickNext(wrapper) // step 5

    expect(wrapper.text()).toContain('Selected library item')
    expect(wrapper.text()).toContain('Item lib-1')
  })
})

describe('OnboardingWizard — done (step 6)', () => {
  it('shows the accomplishments summary for everything completed', async () => {
    const wrapper = await advanceToDone()
    expect(wrapper.text()).toContain("You're all set!")
    expect(wrapper.text()).toContain('My Pipeline')
    expect(wrapper.text()).toContain('Connected')
    expect(wrapper.text()).toContain('My GitHub')
    expect(wrapper.text()).toContain('Inferred schema')
    expect(wrapper.text()).toContain('GitHub Issues')
    expect(wrapper.text()).toContain('Published to schema registry')
  })

  it('Run Pipeline Now warns about the empty payload first, then starts the run on confirm', async () => {
    const wrapper = await advanceToDone()

    await wrapper.find('[data-testid="onboarding-wizard-run-pipeline-now"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(true)
    expect(api.POST).not.toHaveBeenCalledWith('/api/v1/runs', expect.anything())

    await wrapper.find('[data-testid="onboarding-wizard-run-pipeline-now"]').trigger('click')
    await nextTick()
    expect(api.POST).toHaveBeenCalledWith('/api/v1/runs', { body: { pipeline_id: 'pipe-1', input_payload: {} } })
    expect(wrapper.text()).toContain('Pipeline started!')
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(false)
  })

  it('editing the pipeline description clears the empty-run warning', async () => {
    const wrapper = await advanceToDone()
    await wrapper.find('[data-testid="onboarding-wizard-run-pipeline-now"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(true)

    const vm = wrapper.vm as unknown as { wizardState: { pipelineDescription: string } }
    vm.wizardState.pipelineDescription = 'now has a description'
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(false)
  })

  it('the run failure surfaces the error detail (FAR-608 fix)', async () => {
    // runPipeline formats the error envelope with formatApiError, so the
    // detail accompanies the "Failed to start pipeline" message.
    ;(api.POST as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/runs') return { data: undefined, error: { detail: 'runner unavailable' } }
      if (url === '/api/v1/schemas/infer') return { data: inferResponse, error: undefined }
      if (url === '/api/v1/schemas') return { data: { id: 'schema-1' }, error: undefined }
      if (url === '/api/v1/schemas/{schema_id}/versions') return { data: { id: 'ver-1' }, error: undefined }
      if (url === '/api/v1/pipelines') return { data: { id: 'pipe-1', name: 'My Pipeline' }, error: undefined }
      return { data: null, error: undefined }
    })
    const wrapper = await advanceToDone()
    await wrapper.find('[data-testid="onboarding-wizard-run-pipeline-now"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="onboarding-wizard-run-pipeline-now"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Failed to start pipeline')
    expect(wrapper.text()).toContain('runner unavailable')
  })
})
