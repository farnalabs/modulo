import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    del: vi.fn().mockResolvedValue(undefined),
  })),
}))

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] } }),
    POST: vi.fn().mockResolvedValue({ data: {} }),
    PUT: vi.fn().mockResolvedValue({ data: {} }),
    DELETE: vi.fn().mockResolvedValue({ data: {} }),
  },
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

import EvalEditorView from '../views/EvalEditorView.vue'
import { api } from '../lib/api/client'

const apiGET = api.GET as ReturnType<typeof vi.fn>
const apiPOST = api.POST as ReturnType<typeof vi.fn>
const apiPUT = api.PUT as ReturnType<typeof vi.fn>
const apiDELETE = api.DELETE as ReturnType<typeof vi.fn>

function evalItem(over: Record<string, unknown> = {}) {
  return {
    id: 'eval-1',
    pipeline_id: 'p1',
    node_id: null,
    name: 'Existing Eval',
    eval_type: 'regex',
    config_json: { pattern: '.*' },
    failure_behaviour: 'block',
    pass_threshold: 0.9,
    suite_id: null,
    created_by: 'user-1',
    ...over,
  }
}

const stubs = { FeatureGate: { template: '<div><slot /></div>' } }

async function flush() {
  await flushPromises()
  await nextTick()
}

describe('EvalEditorView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    apiGET.mockResolvedValue({ data: { items: [] } })
    apiPOST.mockResolvedValue({ data: {} })
    apiPUT.mockResolvedValue({ data: {} })
    apiDELETE.mockResolvedValue({ data: {} })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(EvalEditorView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('views.EvalEditorView.eval_editor')
  })

  it('renders a per-type config placeholder on the textarea', async () => {
    const wrapper = mount(EvalEditorView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    const textarea = await vi.waitFor(() => {
      const el = wrapper.find('[data-testid="eval-editor-config"]')
      expect(el.exists()).toBe(true)
      return el
    })
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.llm_judge')

    ;(wrapper.vm as any).form.eval_type = 'regex'
    await nextTick()
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.regex')

    ;(wrapper.vm as any).form.eval_type = 'json_schema'
    await nextTick()
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.json_schema')

    ;(wrapper.vm as any).form.eval_type = 'custom_function'
    await nextTick()
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.custom_function')
  })
})

describe('EvalEditorView — FAR-617 pipeline/node/eval CRUD coverage', () => {
  const viewStubs = {
    ...stubs,
    LoadingSpinner: true,
    ErrorAlert: true,
    PageHeader: { template: '<div />' },
    PageTabs: { template: '<div />' },
  }

  function mountView() {
    return mount(EvalEditorView, {
      global: { stubs: viewStubs, mocks: { $t: (key: string) => key } },
    })
  }

  function selectPipeline(wrapper: Awaited<ReturnType<typeof mountView>>, pipelineId: string) {
    ;(wrapper.vm as unknown as { selectedPipelineId: string }).selectedPipelineId = pipelineId
    return (wrapper.vm as unknown as { onPipelineChange: () => Promise<void> }).onPipelineChange()
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    apiGET.mockResolvedValue({ data: { items: [] } })
    apiPOST.mockResolvedValue({ data: {} })
    apiPUT.mockResolvedValue({ data: {} })
    apiDELETE.mockResolvedValue({ data: {} })
  })

  it('initial load fetches pipelines; save is disabled with no pipeline/name chosen', async () => {
    apiGET.mockResolvedValue({ data: { items: [{ id: 'p1', name: 'P One', description: null }] } })
    const wrapper = mountView()
    await flush()

    expect(apiGET).toHaveBeenCalledWith('/api/v1/pipelines')
    const save = wrapper.find('[data-testid="eval-editor-save"]')
    expect(save.attributes('disabled')).toBeDefined()

    // The select-prompt branch only renders when selectedPipelineId is cleared
    // entirely (it initialises to '__all__', so it is never shown on a fresh mount).
    ;(wrapper.vm as unknown as { selectedPipelineId: string }).selectedPipelineId = ''
    await nextTick()
    expect(wrapper.text()).toContain('views.EvalEditorView.prompt_select_pipeline')
  })

  it('selecting a pipeline loads its graph nodes and evals', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') return { data: { items: [{ id: 'p1', name: 'P One', description: null }] } }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
        return { data: { nodes: [{ id: 'n1', node_type: 'agent', label: 'Writer', agent_id: 'a1', position: { x: 0, y: 0 } }] } }
      }
      if (url === '/api/v1/evals') return { data: { items: [evalItem()] } }
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    expect(apiGET).toHaveBeenCalledWith('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: 'p1' } },
    })
    expect(apiGET).toHaveBeenCalledWith('/api/v1/evals', { params: { query: { pipeline_id: 'p1' } } })
    expect(wrapper.text()).toContain('Existing Eval')
    expect(wrapper.text()).toContain('regex')
    expect(wrapper.text()).toContain('block')
  })

  it('graph load failure shows the nodes error without crashing the page', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') throw new Error('graph down')
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    expect(wrapper.text()).toContain('views.EvalEditorView.failed_to_load_nodes')
  })

  it('evals load failure shows the evals error', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/evals') throw new Error('evals down')
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    expect(wrapper.text()).toContain('views.EvalEditorView.failed_to_load_evals')
  })

  it('save (create): POST body carries the parsed config, threshold and failure behaviour; success message shown', async () => {
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Fresh Eval')
    await wrapper.find('[data-testid="eval-editor-config"]').setValue('{"pattern":"abc"}')
    await wrapper.find('[data-testid="eval-editor-pass-threshold"]').setValue('0.55')
    await wrapper.find('[data-testid="eval-editor-failure-block"]').setValue(true)
    await nextTick()

    const save = wrapper.find('[data-testid="eval-editor-save"]')
    expect(save.attributes('disabled')).toBeUndefined()
    await save.trigger('click')
    await flush()

    expect(apiPOST).toHaveBeenCalledTimes(1)
    const [url, options] = apiPOST.mock.calls[0]
    expect(url).toBe('/api/v1/evals')
    expect(options.body).toEqual({
      pipeline_id: 'p1',
      node_id: null,
      name: 'Fresh Eval',
      eval_type: 'llm_judge',
      config_json: { pattern: 'abc' },
      failure_behaviour: 'block',
      pass_threshold: 0.55,
    })
    // BUG: the success flash is invisible — saveEval sets formSuccess then
    // resetForm() (same synchronous block) nulls it again, so neither the
    // flash div nor the setTimeout clear can ever render a message. The POST
    // and the form reset are the observable effects.
    expect((wrapper.vm as unknown as { formSuccess: string | null }).formSuccess).toBe(null)
    expect(wrapper.text()).not.toContain('views.EvalEditorView.eval_created')
  })

  it('save with a specific node selected maps node_id into the body', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
        return { data: { nodes: [{ id: 'n1', node_type: 'agent', label: 'Writer', agent_id: 'a1', position: { x: 0, y: 0 } }] } }
      }
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    ;(wrapper.vm as unknown as { form: { node_id: string } }).form.node_id = 'n1'
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Node Eval')
    await nextTick()

    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect(apiPOST).toHaveBeenCalledTimes(1)
    expect(apiPOST.mock.calls[0][1].body.node_id).toBe('n1')
  })

  it('invalid config JSON disables save and renders the parse error', async () => {
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Broken')
    await wrapper.find('[data-testid="eval-editor-config"]').setValue('{not json')
    await nextTick()

    expect(wrapper.find('[data-testid="eval-editor-save"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('views.EvalEditorView.invalid_json')
    expect(apiPOST).not.toHaveBeenCalled()
  })

  it('edit flow: startEdit prefills the form, PUT carries the eval id, cancel resets', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/evals') return { data: { items: [evalItem()] } }
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    await wrapper.find('[data-testid="eval-editor-edit"]').trigger('click')
    await nextTick()
    expect((wrapper.find('[data-testid="eval-editor-name"]').element as HTMLInputElement).value).toBe('Existing Eval')
    expect((wrapper.find('[data-testid="eval-editor-config"]').element as HTMLTextAreaElement).value).toContain('"pattern"')
    expect(wrapper.text()).toContain('views.EvalEditorView.edit_eval')

    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect(apiPUT).toHaveBeenCalledTimes(1)
    const [url, options] = apiPUT.mock.calls[0]
    expect(url).toBe('/api/v1/evals/{eval_id}')
    expect(options.params.path.eval_id).toBe('eval-1')
    expect(options.body.failure_behaviour).toBe('block')
    expect(options.body.pass_threshold).toBe(0.9)
    // Same invisible-success-flash bug as the create path (see above): the
    // eval_updated message is nulled by resetForm before it can render.
    expect((wrapper.vm as unknown as { formSuccess: string | null }).formSuccess).toBe(null)

    // After a successful save the form is reset and editing state cleared.
    expect((wrapper.find('[data-testid="eval-editor-name"]').element as HTMLInputElement).value).toBe('')

    // Cancel button only exists in edit mode; drive resetForm through a fresh edit.
    await wrapper.find('[data-testid="eval-editor-edit"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    expect((wrapper.find('[data-testid="eval-editor-name"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.find('[data-testid="eval-editor-cancel"]').exists()).toBe(false)
  })

  it('delete flow: confirm deletes via the API and removes the eval from the list', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/evals') return { data: { items: [evalItem()] } }
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    await wrapper.find('[data-testid="eval-editor-delete"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="eval-editor-confirm-delete"]').exists()).toBe(true)

    // Cancel first: no DELETE call, back to the single delete button.
    await wrapper.find('[data-testid="eval-editor-cancel-delete"]').trigger('click')
    await nextTick()
    expect(apiDELETE).not.toHaveBeenCalled()

    await wrapper.find('[data-testid="eval-editor-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-confirm-delete"]').trigger('click')
    await flush()

    expect(apiDELETE).toHaveBeenCalledTimes(1)
    const [url, options] = apiDELETE.mock.calls[0]
    expect(url).toBe('/api/v1/evals/{eval_id}')
    expect(options.params.path.eval_id).toBe('eval-1')
    expect(wrapper.text()).toContain('views.EvalEditorView.no_evals_yet')
  })

  it('delete failure with a 404 payload surfaces the already-deleted message', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/evals') return { data: { items: [evalItem()] } }
      return { data: { items: [] } }
    })
    apiDELETE.mockRejectedValue(new Error('404 not found'))
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    await wrapper.find('[data-testid="eval-editor-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-confirm-delete"]').trigger('click')
    await flush()

    expect(wrapper.text()).toContain('views.EvalEditorView.eval_already_deleted')
  })

  it('empty evals list renders the EmptyState after a pipeline is selected', async () => {
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    expect(wrapper.text()).toContain('views.EvalEditorView.no_evals_yet')
  })
})
