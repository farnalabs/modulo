// Typed-question builder coverage for the eval editor (ADR 066 D4/D19).
// Companion to EvalEditorView.spec.ts (CRUD) and
// EvalEditorViewPolicyGate.spec.ts (policy gate). Assertions target DOM state
// and the emitted request body, never the server's internals.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    PUT: vi.fn(),
    PATCH: vi.fn(),
    DELETE: vi.fn(),
  },
}))

import EvalEditorView from '../views/EvalEditorView.vue'
import enUS from '../locales/en-US.js'
import { api } from '../lib/api/client'
import { buildTypedJudgeConfig, createDefaultDraft, createQuestion } from '../components/eval/typedJudge'
import { usePlanStore } from '../stores/planStore'

const apiGET = api.GET as ReturnType<typeof vi.fn>
const apiPOST = api.POST as ReturnType<typeof vi.fn>
const apiPUT = api.PUT as ReturnType<typeof vi.fn>
const apiDELETE = api.DELETE as ReturnType<typeof vi.fn>

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

const EVAL_PUT_URL = '/api/v1/evals/{eval_id}'

function ok(data: unknown) {
  return { data, error: undefined }
}

function evalItem(over: Record<string, unknown> = {}) {
  return {
    id: 'eval-1',
    pipeline_id: 'p1',
    node_id: null,
    name: 'Existing Eval',
    eval_type: 'regex',
    config_json: { pattern: '.*' },
    pass_threshold: 0.9,
    suite_id: null,
    created_by: 'user-1',
    ...over,
  }
}

let evalsList: Record<string, unknown>[] = []
let modelBackends: Record<string, unknown>[] = []

function installRouter() {
  apiGET.mockImplementation(async (url: string) => {
    if (url === '/api/v1/pipelines') {
      return ok({ items: [{ id: 'p1', name: 'P One', description: null }] })
    }
    if (url === '/api/v1/pipelines/{pipeline_id}/graph') return ok({ nodes: [] })
    if (url === '/api/v1/evals') return ok({ items: evalsList })
    if (url === '/api/v1/model-backends') return ok({ items: modelBackends })
    if (url === '/api/v1/admin/feature-flags') {
      return ok({
        license: { tier: 'community' },
        dev_mode: false,
        flags: [{ name: 'eval_system', currently_active: true }],
      })
    }
    if (url === '/api/v1/admin/license') return ok({ tier: 'community' })
    if (url === '/api/v1/admin/tiers') return ok({ tiers: [] })
    return ok({ items: [] })
  })
  apiPOST.mockImplementation(async (url: string) => {
    if (url === '/api/v1/evals') return ok({ id: 'new-eval' })
    return ok({})
  })
  apiPUT.mockImplementation(async () => ok({}))
  apiDELETE.mockImplementation(async () => ok(null))
}

const viewStubs = {
  FeatureGate: { template: '<div><slot /></div>' },
  LoadingSpinner: true,
  ErrorAlert: true,
  PageHeader: { template: '<div />' },
}

function mountView() {
  return mount(EvalEditorView, { global: { plugins: [i18n], stubs: viewStubs } })
}

async function flush() {
  await flushPromises()
  await nextTick()
}

type ViewWrapper = ReturnType<typeof mountView>

async function selectPipeline(wrapper: ViewWrapper) {
  ;(wrapper.vm as unknown as { selectedPipelineId: string }).selectedPipelineId = 'p1'
  await (wrapper.vm as unknown as { onPipelineChange: () => Promise<void> }).onPipelineChange()
}

async function openEditor(wrapper: ViewWrapper, evalId: string) {
  const index = evalsList.findIndex((item) => item.id === evalId)
  await wrapper.findAll('[data-testid="eval-editor-edit"]')[index].trigger('click')
  await flush()
}

function typedDraft(wrapper: ViewWrapper) {
  return (wrapper.vm as unknown as { typedDraft: ReturnType<typeof createDefaultDraft> }).typedDraft
}

function callTo(url: string) {
  return apiPOST.mock.calls.find((call) => call[0] === url)
}

describe('EvalEditorView — typed question builder', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    const plan = usePlanStore()
    plan.features = { eval_system: true }
    plan.loaded = true
    evalsList = []
    modelBackends = []
    installRouter()
  })

  it('offers only the two mechanisms on the first axis, then the backing on the second', async () => {
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as {
      mechanismOptions: { value: string; label: string }[]
      onMechanismChange: (value: string) => void
    }
    expect(vm.mechanismOptions.map((option) => option.label)).toEqual([
      'Typed questions',
      'Deterministic',
    ])

    // Typed questions is the default mechanism: the backing axis is present and
    // the deterministic sub-axis is not.
    expect(wrapper.find('[data-testid="eval-editor-backing-kind"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="eval-editor-deterministic-type"]').exists()).toBe(false)

    vm.onMechanismChange('deterministic')
    await nextTick()
    expect(wrapper.find('[data-testid="eval-editor-backing-kind"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="eval-editor-deterministic-type"]').exists()).toBe(true)
    expect(wrapper.find('textarea[data-testid="eval-editor-config"]').exists()).toBe(true)

    vm.onMechanismChange('typed_questions')
    await nextTick()
    expect(wrapper.find('[data-testid="eval-editor-backing-kind"]').exists()).toBe(true)
    expect(wrapper.find('div[data-testid="eval-editor-config"]').exists()).toBe(true)
  })

  it('labels a model-backed judge with the model, vendor and self-reported confidence — never the decision-model phrase', async () => {
    modelBackends = [
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'Claude',
        display_name: 'Claude',
        provider: 'anthropic',
        model_id: 'claude-3-5-sonnet',
      },
    ]
    const wrapper = mountView()
    await flush()
    typedDraft(wrapper).modelBackendId = '11111111-1111-4111-8111-111111111111'
    await nextTick()

    const preview = wrapper.find('[data-testid="eval-editor-backing-preview"]')
    expect(preview.text()).toBe(
      'Typed questions — claude-3-5-sonnet via anthropic (LLM, self-reported confidence)',
    )
    expect(preview.text()).not.toContain('System One')
  })

  it('labels a decision-model backing from the single locale model-class value', async () => {
    const wrapper = mountView()
    await flush()
    typedDraft(wrapper).backingKind = 'decision_model'
    typedDraft(wrapper).decisionVendor = 'Jev'
    await nextTick()

    expect(wrapper.find('[data-testid="eval-editor-backing-preview"]').text()).toBe(
      'Typed questions — Jev (System One model)',
    )
  })

  it('blocks a derivation verdict and explains why typed judges are the wrong instrument', async () => {
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Derivation eval')
    typedDraft(wrapper).verdictKind = 'derivation'
    await nextTick()

    const warning = wrapper.find('[data-testid="eval-editor-derivation-warning"]')
    expect(warning.exists()).toBe(true)
    expect(warning.text()).toContain('weakest')
    expect(warning.text()).toContain('must be computed')
    expect(wrapper.find('[data-testid="eval-editor-client-errors"]').text()).toContain(
      'Derivation checks are not supported by typed questions',
    )
    expect(wrapper.find('[data-testid="eval-editor-save"]').attributes('disabled')).toBeDefined()
    expect(callTo('/api/v1/evals')).toBeUndefined()
  })

  it('mirrors the server bounds client-side: removing every question blocks the save', async () => {
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('No questions')
    expect(wrapper.find('[data-testid="eval-editor-save"]').attributes('disabled')).toBeUndefined()

    await wrapper.find('[data-testid="eval-editor-question-remove"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="eval-editor-client-errors"]').text()).toContain(
      'Add at least one question.',
    )
    expect(wrapper.find('[data-testid="eval-editor-save"]').attributes('disabled')).toBeDefined()
  })

  it('surfaces the server typed 422 code inline instead of a blind submit failure', async () => {
    apiPOST.mockResolvedValueOnce({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:validation_error',
        title: 'Validation Error',
        status: 422,
        detail: 'verdict_kind=derivation is not supported by typed judgment in v1',
        code: 'judge_verdict_kind_unsupported',
      },
    })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Server rejects')
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    const serverError = wrapper.find('[data-testid="eval-editor-server-error"]')
    expect(serverError.exists()).toBe(true)
    expect(serverError.text()).toContain('Derivation checks are not supported by typed questions.')
    expect(serverError.text()).toContain(
      'verdict_kind=derivation is not supported by typed judgment in v1',
    )
    expect((wrapper.vm as unknown as { formError: string | null }).formError).toBeNull()
  })

  it('keeps a legacy prompt judge read-only and sends its config unchanged on a metadata edit', async () => {
    evalsList = [
      evalItem({
        eval_type: 'model_judge',
        config_json: { field: 'output', instructions: 'Legacy prompt' },
      }),
    ]
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    expect(wrapper.find('[data-testid="eval-editor-legacy-notice"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="eval-editor-backing-kind"]').exists()).toBe(false)

    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    const put = apiPUT.mock.calls.find((call) => call[0] === EVAL_PUT_URL)
    expect(put?.[1].body.config_json).toEqual({ field: 'output', instructions: 'Legacy prompt' })
  })

  it('migrates a legacy judge only on an explicit action, then emits the typed payload', async () => {
    evalsList = [
      evalItem({
        eval_type: 'model_judge',
        config_json: { field: 'output.text', instructions: 'Legacy prompt' },
      }),
    ]
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-testid="eval-editor-migrate-legacy"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="eval-editor-legacy-notice"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="eval-editor-backing-kind"]').exists()).toBe(true)
    // The legacy judged field is carried into the migrated draft.
    expect(typedDraft(wrapper).field).toBe('output.text')

    // Capture the emitted payload before the save resets the form.
    const expected = buildTypedJudgeConfig(typedDraft(wrapper))
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    const put = apiPUT.mock.calls.find((call) => call[0] === EVAL_PUT_URL)
    expect(put?.[1].body.config_json).toEqual(expected)
    expect(put?.[1].body.config_json.answer_contract).toBe('typed_questions')
  })

  it('seeds a legal answer space when a question type changes', async () => {
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as {
      typedDraft: { questions: ReturnType<typeof createQuestion>[] }
      setQuestionType: (question: ReturnType<typeof createQuestion>, value: string) => void
    }
    vm.setQuestionType(vm.typedDraft.questions[0], 'choice')
    await nextTick()
    expect(wrapper.findAll('[data-testid="eval-editor-question-option-key"]')).toHaveLength(2)

    vm.setQuestionType(vm.typedDraft.questions[0], 'score')
    await nextTick()
    expect(wrapper.findAll('[data-testid="eval-editor-question-level-score"]')).toHaveLength(2)
  })

  it('sends the builder payload as config_json on create', async () => {
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Typed eval')

    const expected = buildTypedJudgeConfig(typedDraft(wrapper))
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    const post = callTo('/api/v1/evals')
    expect(post?.[1].body.config_json).toEqual(expected)
    expect(post?.[1].body.config_json.answer_contract).toBe('typed_questions')
    expect(post?.[1].body.config_json).not.toHaveProperty('pass_threshold')
  })
})
