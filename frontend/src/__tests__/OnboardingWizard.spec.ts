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

// Drives the wizard to step 6 (Telemetry), creating the pipeline on the way.
async function advanceToTelemetry() {
  const wrapper = await advanceToStep5()
  await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
  await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
  await nextTick()
  await clickNext(wrapper) // -> step 6 (telemetry)
  return wrapper
}

// Drives the wizard to step 7 (Done) by creating the pipeline and skipping telemetry.
async function advanceToDone() {
  const wrapper = await advanceToTelemetry()
  // Telemetry step: click skip-to-end or next to advance to Done
  await clickNext(wrapper) // -> step 7 (Done)
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

    // Next becomes available after creation (telemetry step follows).
    const nextBtn = wrapper.find('[data-testid="onboarding-wizard-next"]')
    expect(nextBtn.attributes('disabled')).toBeUndefined()
    expect(nextBtn.text()).toContain('Next')
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

describe('OnboardingWizard — telemetry step (FAR-1131)', () => {
  it('shows telemetry step after Wire Pipeline', async () => {
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    await clickNext(wrapper) // -> step 6 (telemetry)
    expect(wrapper.text()).toContain('Help Improve Modulo')
    expect(wrapper.text()).toContain('Enable Telemetry')
    expect(wrapper.text()).toContain('Skip for Now')
  })

  it('loads telemetry status when reaching the telemetry step', async () => {
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    await clickNext(wrapper) // -> step 6 (telemetry)
    await nextTick()
    expect(api.GET).toHaveBeenCalledWith('/api/v1/admin/telemetry')
  })

  it('enable button calls PUT with enabled=true', async () => {
    ;(api.PUT as Mock).mockResolvedValue({ data: { enabled: true }, error: undefined })
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    await clickNext(wrapper) // -> step 6 (telemetry)
    await nextTick()
    await wrapper.find('[data-testid="onboarding-wizard-telemetry-enable"]').trigger('click')
    await flushPromises()
    expect(api.PUT).toHaveBeenCalledWith('/api/v1/admin/telemetry', { body: { enabled: true } })
  })

  it('skip button advances to Done without calling PUT', async () => {
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    await clickNext(wrapper) // -> step 6 (telemetry)
    await nextTick()
    await wrapper.find('[data-testid="onboarding-wizard-telemetry-skip"]').trigger('click')
    await nextTick()
    expect(api.PUT).not.toHaveBeenCalled()
    // Should now be on Done step
    expect(wrapper.text()).toContain("You're all set!")
  })

  it('telemetry step shows what-is-collected items', async () => {
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    await clickNext(wrapper) // -> step 6 (telemetry)
    await nextTick()
    expect(wrapper.text()).toContain('pipeline run counts')
    expect(wrapper.text()).toContain('Error category')
    expect(wrapper.text()).toContain('features are used')
  })

  it('telemetry step shows can-change-later notice', async () => {
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    await clickNext(wrapper) // -> step 6 (telemetry)
    await nextTick()
    expect(wrapper.text()).toContain('change this anytime')
  })

  it('surfaces the telemetry status load failure from the error envelope', async () => {
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    const defaultGet = (api.GET as Mock).getMockImplementation()!
    ;(api.GET as Mock).mockImplementation(async (url: string, opts?: unknown) => {
      if (url === '/api/v1/admin/telemetry') return { data: undefined, error: { detail: 'telemetry offline' } }
      return defaultGet(url, opts)
    })
    await clickNext(wrapper) // -> step 6 (telemetry) triggers loadTelemetryStatus
    await flushPromises()
    expect(wrapper.text()).toContain('telemetry offline')
  })

  it('surfaces the telemetry status load exception from the catch path', async () => {
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    const defaultGet = (api.GET as Mock).getMockImplementation()!
    ;(api.GET as Mock).mockImplementation(async (url: string, opts?: unknown) => {
      if (url === '/api/v1/admin/telemetry') throw new Error('telemetry unreachable')
      return defaultGet(url, opts)
    })
    await clickNext(wrapper) // -> step 6 (telemetry) triggers loadTelemetryStatus
    await flushPromises()
    expect(wrapper.text()).toContain('telemetry unreachable')
  })

  it('surfaces the telemetry save failure from the error envelope', async () => {
    const wrapper = await advanceToTelemetry()
    await flushPromises()
    ;(api.PUT as Mock).mockResolvedValue({ data: undefined, error: { detail: 'save rejected' } })
    await wrapper.find('[data-testid="onboarding-wizard-telemetry-enable"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('save rejected')
  })

  it('surfaces the telemetry save exception from the catch path', async () => {
    const wrapper = await advanceToTelemetry()
    await flushPromises()
    ;(api.PUT as Mock).mockRejectedValue(new Error('telemetry write exploded'))
    await wrapper.find('[data-testid="onboarding-wizard-telemetry-enable"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('telemetry write exploded')
  })

  it('renders an inline ErrorAlert with a Retry that re-loads telemetry status', async () => {
    const wrapper = await advanceToStep5()
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    const defaultGet = (api.GET as Mock).getMockImplementation()!
    let telemetryCalls = 0
    ;(api.GET as Mock).mockImplementation(async (url: string, opts?: unknown) => {
      if (url === '/api/v1/admin/telemetry') {
        telemetryCalls += 1
        if (telemetryCalls === 1) return { data: undefined, error: { detail: 'telemetry offline' } }
        return { data: { enabled: false }, error: undefined }
      }
      return defaultGet(url, opts)
    })
    await clickNext(wrapper) // -> step 6 triggers loadTelemetryStatus (fails)
    await flushPromises()
    expect(wrapper.text()).toContain('telemetry offline')
    const retry = wrapper.findAll('button').find((b) => b.text() === 'Retry')
    expect(retry).toBeTruthy()
    await retry!.trigger('click')
    await flushPromises()
    expect(telemetryCalls).toBe(2)
    expect(wrapper.text()).not.toContain('telemetry offline')
  })

  it('renders an inline ErrorAlert with a Retry that re-saves telemetry', async () => {
    const wrapper = await advanceToTelemetry()
    await flushPromises()
    let putCalls = 0
    ;(api.PUT as Mock).mockImplementation(async () => {
      putCalls += 1
      if (putCalls === 1) return { data: undefined, error: { detail: 'save rejected' } }
      return { data: { enabled: true }, error: undefined }
    })
    await wrapper.find('[data-testid="onboarding-wizard-telemetry-enable"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('save rejected')
    const retry = wrapper.findAll('button').find((b) => b.text() === 'Retry')
    expect(retry).toBeTruthy()
    await retry!.trigger('click')
    await flushPromises()
    expect(putCalls).toBe(2)
    expect(wrapper.text()).not.toContain('save rejected')
  })

  it('ignores a second telemetry save while one is already in flight', async () => {
    const wrapper = await advanceToTelemetry()
    await flushPromises()
    let resolvePut: (value: unknown) => void = () => {}
    ;(api.PUT as Mock).mockReturnValue(
      new Promise((resolve) => {
        resolvePut = resolve
      }),
    )
    const vm = wrapper.vm as unknown as { saveTelemetry: (enabled: boolean) => Promise<void> }
    const first = vm.saveTelemetry(true)
    const second = vm.saveTelemetry(true)
    resolvePut({ data: { enabled: true }, error: undefined })
    await first
    await second
    expect(api.PUT).toHaveBeenCalledTimes(1)
  })

  it('renders the current telemetry status returned by the GET', async () => {
    const wrapper = await advanceToStep5()
    const defaultGet = (api.GET as Mock).getMockImplementation()!
    ;(api.GET as Mock).mockImplementation(async (url: string, opts?: unknown) => {
      if (url === '/api/v1/admin/telemetry') return { data: { enabled: true }, error: undefined }
      return defaultGet(url, opts)
    })
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    await clickNext(wrapper) // -> step 6 (telemetry) loads status
    await flushPromises()
    expect(wrapper.find('[data-testid="onboarding-wizard-telemetry-status"]').text()).toContain('currently on')
  })

  it('softens a 403 into an admin-only note and hides the enable button', async () => {
    const wrapper = await advanceToStep5()
    const defaultGet = (api.GET as Mock).getMockImplementation()!
    ;(api.GET as Mock).mockImplementation(async (url: string, opts?: unknown) => {
      if (url === '/api/v1/admin/telemetry') {
        return { data: undefined, error: { detail: 'requires system.config.manage' }, response: { status: 403 } }
      }
      return defaultGet(url, opts)
    })
    await wrapper.find('[data-testid="onboarding-wizard-pipeline-name"]').setValue('My Pipeline')
    await wrapper.find('[data-testid="onboarding-wizard-create-pipeline"]').trigger('click')
    await nextTick()
    await clickNext(wrapper) // -> step 6 (telemetry)
    await flushPromises()
    expect(wrapper.find('[data-testid="onboarding-wizard-telemetry-forbidden"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="onboarding-wizard-telemetry-enable"]').exists()).toBe(false)
  })

  it('confirms success after enabling telemetry', async () => {
    ;(api.PUT as Mock).mockResolvedValue({ data: { enabled: true }, error: undefined })
    const wrapper = await advanceToTelemetry()
    await flushPromises()
    await wrapper.find('[data-testid="onboarding-wizard-telemetry-enable"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="onboarding-wizard-telemetry-saved"]').text()).toContain('Telemetry enabled')
  })

  it('softens a 403 on save into an admin-only note and hides the enable button', async () => {
    const wrapper = await advanceToTelemetry()
    await flushPromises()
    ;(api.PUT as Mock).mockResolvedValue({
      data: undefined,
      error: { detail: 'requires system.config.manage' },
      response: { status: 403 },
    })
    await wrapper.find('[data-testid="onboarding-wizard-telemetry-enable"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="onboarding-wizard-telemetry-forbidden"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="onboarding-wizard-telemetry-enable"]').exists()).toBe(false)
  })

  it('falls back to the requested value when the save response omits enabled', async () => {
    ;(api.PUT as Mock).mockResolvedValue({ data: undefined, error: undefined })
    const wrapper = await advanceToTelemetry()
    await flushPromises()
    await wrapper.find('[data-testid="onboarding-wizard-telemetry-enable"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="onboarding-wizard-telemetry-saved"]').text()).toContain('Telemetry enabled')
  })
})
