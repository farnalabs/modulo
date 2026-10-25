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
import {
  MAX_CHOICE_OPTIONS,
  MAX_SCORE_LEVELS,
  buildTypedJudgeConfig,
  createDefaultDraft,
  createQuestion,
} from '../components/eval/typedJudge'
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

interface RouterOverrides {
  modelBackends?: () => unknown
}

function installRouter(overrides: RouterOverrides = {}) {
  apiGET.mockImplementation(async (url: string) => {
    if (url === '/api/v1/pipelines') {
      return ok({ items: [{ id: 'p1', name: 'P One', description: null }] })
    }
    if (url === '/api/v1/pipelines/{pipeline_id}/graph') return ok({ nodes: [] })
    if (url === '/api/v1/evals') return ok({ items: evalsList })
    if (url === '/api/v1/model-backends') {
      if (overrides.modelBackends) return overrides.modelBackends()
      return ok({ items: modelBackends })
    }
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

/**
 * Open a real PrimeVue Select and pick an option. PrimeVue 5 moves selection
 * on click (mousedown only focuses), so follow a real pointer. This exercises
 * the `#option` slot and the `update:model-value` handler, not just render.
 */
async function chooseOption(wrapper: ViewWrapper, testid: string, value: string) {
  const select = wrapper.find(`[data-testid="${testid}"]`)
  expect(select.exists(), `select ${testid} exists`).toBe(true)
  await select.trigger('click')
  await flush()
  const option = wrapper.find(`.p-select-option [data-value="${value}"]`)
  expect(option.exists(), `option ${value} for ${testid} exists`).toBe(true)
  await option.trigger('mousedown')
  await option.trigger('click')
  await flush()
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

// ---------------------------------------------------------------------------
// Interaction coverage: drive the real controls so the template's v-model
// handlers, scoped #option slots and every panel branch are exercised.
// ---------------------------------------------------------------------------

const TYPED_CONFIG = {
  answer_contract: 'typed_questions',
  field: 'output',
  backing: { kind: 'model_backend' },
  questions: [
    {
      id: 'verdict',
      type: 'noul',
      instructions: '',
      criteria: [],
      weight: 1,
      required: true,
      min_pass_confidence: 0.5,
    },
  ],
  pass_rule: { kind: 'weighted_score_at_least', threshold: 0.5 },
  verdict_kind: 'assessment',
}

describe('EvalEditorView — typed builder interaction coverage', () => {
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

  it('drives the mechanism and deterministic axes through the real selects', async () => {
    const wrapper = mountView()
    await flush()

    // Empty backend list renders its own guidance (not an error).
    expect(wrapper.text()).toContain('No model backends are configured for this organisation yet.')

    await chooseOption(wrapper, 'eval-editor-eval-type', 'deterministic')
    expect(wrapper.find('textarea[data-testid="eval-editor-config"]').exists()).toBe(true)
    expect((wrapper.vm as any).form.eval_type).toBe('regex')

    await chooseOption(wrapper, 'eval-editor-deterministic-type', 'json_schema')
    expect((wrapper.vm as any).form.eval_type).toBe('json_schema')
    expect((wrapper.vm as any).form.config_json).toContain('"schema"')

    // An unknown deterministic type is ignored rather than corrupting the form.
    ;(wrapper.vm as any).onDeterministicTypeChange('bogus')
    expect((wrapper.vm as any).form.eval_type).toBe('json_schema')

    // Empty config is not saveable and is not reported as invalid JSON.
    ;(wrapper.vm as any).form.config_json = ''
    await nextTick()
    expect(wrapper.find('[data-testid="eval-editor-save"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).not.toContain('Invalid JSON')

    await chooseOption(wrapper, 'eval-editor-eval-type', 'typed_questions')
    expect(wrapper.find('[data-testid="eval-editor-backing-kind"]').exists()).toBe(true)
  })

  it('drives the backing axis and labels both backings', async () => {
    modelBackends = [
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'Claude fallback name',
        display_name: '',
        provider: 'anthropic',
        model_id: 'claude-3-5-sonnet',
      },
    ]
    const wrapper = mountView()
    await flush()

    // Non-empty list with nothing selected.
    expect(wrapper.text()).toContain('No backend selected: the judge will record a configuration error')

    await chooseOption(wrapper, 'eval-editor-backing-model', '11111111-1111-4111-8111-111111111111')
    expect(wrapper.find('[data-testid="eval-editor-backing-preview"]').text()).toBe(
      'Typed questions — claude-3-5-sonnet via anthropic (LLM, self-reported confidence)',
    )

    // A stored id the loaded list cannot resolve is not shown as "none selected".
    typedDraft(wrapper).modelBackendId = 'unknown-backend'
    await nextTick()
    expect(wrapper.find('[data-testid="eval-editor-backing-preview"]').text()).toBe(
      'Typed questions — model backend unknown-backend (unavailable)',
    )

    await chooseOption(wrapper, 'eval-editor-backing-kind', 'decision_model')
    await wrapper.find('[data-testid="eval-editor-backing-vendor"]').setValue('Jev')
    await wrapper.find('[data-testid="eval-editor-backing-model-class"]').setValue('system_one')
    expect(typedDraft(wrapper).decisionModelClass).toBe('system_one')
    expect(wrapper.find('[data-testid="eval-editor-backing-preview"]').text()).toBe(
      'Typed questions — Jev (System One model)',
    )

    // Empty vendor falls back to the generic class label.
    await wrapper.find('[data-testid="eval-editor-backing-vendor"]').setValue('')
    expect(wrapper.find('[data-testid="eval-editor-backing-preview"]').text()).toBe(
      'Typed questions — decision model (System One model)',
    )

    await chooseOption(wrapper, 'eval-editor-backing-kind', 'model_backend')
    typedDraft(wrapper).modelBackendId = ''
    await nextTick()
    expect(wrapper.find('[data-testid="eval-editor-backing-preview"]').text()).toBe(
      'Typed questions — no model backend selected',
    )
  })

  it('drives the verdict, field, context and timeout controls', async () => {
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Controls')

    await chooseOption(wrapper, 'eval-editor-verdict-kind', 'derivation')
    expect(wrapper.find('[data-testid="eval-editor-derivation-warning"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="eval-editor-save"]').attributes('disabled')).toBeDefined()
    await chooseOption(wrapper, 'eval-editor-verdict-kind', 'assessment')

    await wrapper.find('[data-testid="eval-editor-field"]').setValue('output.text')
    expect(typedDraft(wrapper).field).toBe('output.text')
    await wrapper.find('[data-testid="eval-editor-judge-whole-output"]').setValue(true)
    expect(
      (wrapper.find('[data-testid="eval-editor-field"]').element as HTMLInputElement).disabled,
    ).toBe(true)
    await wrapper.find('[data-testid="eval-editor-context-fields"]').setValue('input, output.metadata')
    await wrapper.find('[data-testid="eval-editor-pass-rule-threshold"]').setValue('0.7')
    await wrapper.find('[data-testid="eval-editor-timeout-question"]').setValue('12')
    await wrapper.find('[data-testid="eval-editor-timeout-definition"]').setValue('50')

    // Vue's v-model casts a number input to a number (a cleared field stays '').
    expect(typedDraft(wrapper).threshold).toBe(0.7)
    expect(typedDraft(wrapper).perQuestionSeconds).toBe(12)
    expect(typedDraft(wrapper).perDefinitionSeconds).toBe(50)
  })

  it('edits a noul question and covers add/remove + id generation', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="eval-editor-question-id"]').setValue('accuracy')
    await wrapper.find('[data-testid="eval-editor-question-instructions"]').setValue('Is it accurate?')
    await wrapper
      .find('[data-testid="eval-editor-question-criteria"]')
      .setValue('No facts invented\nNo hedging')
    await wrapper.find('[data-testid="eval-editor-question-weight"]').setValue('2')
    await wrapper.find('[data-testid="eval-editor-question-min-confidence"]').setValue('0.8')
    await wrapper.find('[data-testid="eval-editor-question-required"]').setValue(true)

    expect(typedDraft(wrapper).questions[0]).toMatchObject({
      id: 'accuracy',
      instructions: 'Is it accurate?',
      weight: 2,
      minPassConfidence: 0.8,
      required: true,
    })

    await wrapper.find('[data-testid="eval-editor-question-add"]').trigger('click')
    await nextTick()
    expect(typedDraft(wrapper).questions).toHaveLength(2)

    // Force the id-collision loop in nextQuestionId.
    typedDraft(wrapper).questions[1].id = 'q3'
    expect((wrapper.vm as any).nextQuestionId()).toBe('q4')
    await wrapper.find('[data-testid="eval-editor-question-add"]').trigger('click')
    await nextTick()
    expect(typedDraft(wrapper).questions).toHaveLength(3)

    await wrapper.findAll('[data-testid="eval-editor-question-remove"]')[2].trigger('click')
    await nextTick()
    expect(typedDraft(wrapper).questions).toHaveLength(2)
  })

  it('edits a choice question, including option add/remove and the cap', async () => {
    const wrapper = mountView()
    await flush()

    await chooseOption(wrapper, 'eval-editor-question-type', 'choice')
    expect(wrapper.findAll('[data-testid="eval-editor-question-option-key"]')).toHaveLength(2)

    const keys = wrapper.findAll('[data-testid="eval-editor-question-option-key"]')
    await keys[0].setValue('neutral')
    await keys[1].setValue('friendly')
    const descriptions = wrapper.findAll('[data-testid="eval-editor-question-option-description"]')
    await descriptions[0].setValue('Plain')
    await descriptions[1].setValue('Warm')
    await wrapper.findAll('[data-testid="eval-editor-question-option-passing"]')[0].setValue(true)
    const scores = wrapper.findAll('[data-testid="eval-editor-question-option-score"]')
    await scores[0].setValue('1')
    await scores[1].setValue('0.5')

    await wrapper.find('[data-testid="eval-editor-question-add-option"]').trigger('click')
    await nextTick()
    expect(wrapper.findAll('[data-testid="eval-editor-question-option-key"]')).toHaveLength(3)

    await wrapper.findAll('[data-testid="eval-editor-question-remove-option"]')[2].trigger('click')
    await nextTick()
    expect(wrapper.findAll('[data-testid="eval-editor-question-option-key"]')).toHaveLength(2)

    // Cap branch: at MAX the add control is disabled and addOption is a no-op.
    const question = typedDraft(wrapper).questions[0]
    question.options = Array.from({ length: MAX_CHOICE_OPTIONS }, (_, index) => ({
      key: `k${index}`,
      description: '',
      passing: index === 0,
      score: '',
    }))
    await nextTick()
    expect(
      wrapper.find('[data-testid="eval-editor-question-add-option"]').attributes('disabled'),
    ).toBeDefined()
    ;(wrapper.vm as any).addOption(question)
    expect(question.options).toHaveLength(MAX_CHOICE_OPTIONS)

    const built = buildTypedJudgeConfig(typedDraft(wrapper))
    expect((built.questions as Record<string, unknown>[])[0].passing_keys).toEqual(['k0'])
  })

  it('edits a score question, including level add/remove and the cap', async () => {
    const wrapper = mountView()
    await flush()

    await chooseOption(wrapper, 'eval-editor-question-type', 'score')
    expect(wrapper.findAll('[data-testid="eval-editor-question-level-score"]')).toHaveLength(2)

    const scores = wrapper.findAll('[data-testid="eval-editor-question-level-score"]')
    await scores[0].setValue('0')
    await scores[1].setValue('2')
    const descriptions = wrapper.findAll('[data-testid="eval-editor-question-level-description"]')
    await descriptions[0].setValue('Bad')
    await descriptions[1].setValue('Good')
    await wrapper.find('[data-testid="eval-editor-question-pass-from"]').setValue('2')

    await wrapper.find('[data-testid="eval-editor-question-add-level"]').trigger('click')
    await nextTick()
    expect(wrapper.findAll('[data-testid="eval-editor-question-level-score"]')).toHaveLength(3)
    await wrapper.findAll('[data-testid="eval-editor-question-remove-level"]')[2].trigger('click')
    await nextTick()
    expect(wrapper.findAll('[data-testid="eval-editor-question-level-score"]')).toHaveLength(2)

    const question = typedDraft(wrapper).questions[0]
    question.levels = Array.from({ length: MAX_SCORE_LEVELS }, (_, index) => ({
      score: String(index),
      description: '',
    }))
    await nextTick()
    expect(
      wrapper.find('[data-testid="eval-editor-question-add-level"]').attributes('disabled'),
    ).toBeDefined()
    ;(wrapper.vm as any).addLevel(question)
    expect(question.levels).toHaveLength(MAX_SCORE_LEVELS)
  })

  it('ignores an unknown question type', async () => {
    const wrapper = mountView()
    await flush()
    ;(wrapper.vm as any).setQuestionType(typedDraft(wrapper).questions[0], 'bogus')
    expect(typedDraft(wrapper).questions[0].type).toBe('noul')
  })

  it('migrates a legacy config without a field to the default judged field', async () => {
    evalsList = [evalItem({ eval_type: 'model_judge', config_json: { instructions: 'Legacy' } })]
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    // Legacy mode suppresses the client-error list.
    expect(wrapper.find('[data-testid="eval-editor-client-errors"]').exists()).toBe(false)
    await wrapper.find('[data-testid="eval-editor-migrate-legacy"]').trigger('click')
    await nextTick()
    expect(typedDraft(wrapper).field).toBe('output')
  })

  it('shows an inline error when model backends fail to load', async () => {
    installRouter({ modelBackends: () => ({ data: undefined, error: { status: 500, detail: 'down' } }) })
    const wrapper = mountView()
    await flush()
    expect(wrapper.find('[data-testid="eval-editor-backing-kind"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Failed to load model backends.')
  })

  it('treats a thrown model-backend request as non-fatal', async () => {
    installRouter({
      modelBackends: () => {
        throw new Error('network down')
      },
    })
    const wrapper = mountView()
    await flush()
    expect(wrapper.text()).toContain('Failed to load model backends.')
    expect(wrapper.find('[data-testid="eval-editor-backing-kind"]').exists()).toBe(true)
  })

  it('tolerates a model-backend response with no payload', async () => {
    installRouter({ modelBackends: () => ({ data: undefined, error: undefined }) })
    const wrapper = mountView()
    await flush()
    expect(wrapper.text()).toContain('No model backends are configured')
  })

  it('surfaces a non-judge write failure in the generic form error', async () => {
    apiPOST.mockResolvedValueOnce({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:internal_error',
        title: 'Internal Error',
        status: 500,
        detail: 'boom',
      },
    })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Generic failure')
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect((wrapper.vm as any).formError).toBe('boom')
    expect(wrapper.find('[data-testid="eval-editor-server-error"]').exists()).toBe(false)
  })

  it('surfaces a coded non-judge failure in the generic form error', async () => {
    apiPOST.mockResolvedValueOnce({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:not_found',
        title: 'Not Found',
        status: 404,
        detail: 'pipeline missing',
        code: 'pipeline_not_found',
      },
    })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Coded failure')
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect((wrapper.vm as any).formError).toBe('pipeline missing')
    expect(wrapper.find('[data-testid="eval-editor-server-error"]').exists()).toBe(false)
  })

  it('renders an unrecognised judge code with the generic heading', async () => {
    apiPOST.mockResolvedValueOnce({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:validation_error',
        title: 'Validation Error',
        status: 422,
        detail: 'a future judge rule failed',
        code: 'judge_future_code',
      },
    })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Future code')
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    const serverError = wrapper.find('[data-testid="eval-editor-server-error"]')
    expect(serverError.exists()).toBe(true)
    expect(serverError.text()).toContain('The server rejected this judge configuration.')
    expect(serverError.text()).toContain('a future judge rule failed')
  })

  it('surfaces a failed update on the edit path inline', async () => {
    evalsList = [
      evalItem({ id: 'eval-typed', eval_type: 'model_judge', config_json: TYPED_CONFIG }),
    ]
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-typed')

    apiPUT.mockResolvedValueOnce({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:validation_error',
        title: 'Validation Error',
        status: 422,
        detail: 'bad config',
        code: 'judge_config_invalid',
      },
    })
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect(wrapper.find('[data-testid="eval-editor-server-error"]').text()).toContain('bad config')
  })

  it('renders the backend-composed judge display and edits a typed definition', async () => {
    evalsList = [
      evalItem({
        id: 'eval-typed',
        name: 'Typed',
        eval_type: 'model_judge',
        config_json: TYPED_CONFIG,
        judge: { display: 'Typed questions — Jev (System One model)' },
      }),
      evalItem({ id: 'eval-plain', name: 'Plain', eval_type: 'regex' }),
    ]
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()

    expect(wrapper.find('[data-testid="eval-editor-judge-display"]').text()).toBe(
      'Typed questions — Jev (System One model)',
    )

    await openEditor(wrapper, 'eval-typed')
    expect(typedDraft(wrapper).field).toBe('output')
    expect(
      (wrapper.find('[data-testid="eval-editor-question-id"]').element as HTMLInputElement).value,
    ).toBe('verdict')
  })

  it('falls back to regex for a deterministic eval type the builder does not offer', async () => {
    evalsList = [evalItem({ eval_type: 'human_set', config_json: { items: [] } })]
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    expect((wrapper.vm as any).deterministicType).toBe('regex')
  })

  it('has no backing preview when the mechanism is deterministic', async () => {
    const wrapper = mountView()
    await flush()
    await chooseOption(wrapper, 'eval-editor-eval-type', 'deterministic')
    expect((wrapper.vm as any).backingPreview).toBe('')
  })
})
