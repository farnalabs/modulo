import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import OutputValidationTab from '../components/pipeline/composite/OutputValidationTab.vue'

vi.mock('../lib/formatDate', () => ({
  formatDateShort: (d: Date) => d.toLocaleDateString('en-US'),
}))

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      components: {
        pipeline: {
          composite: {
            OutputValidationTab: {
              output_validation: 'Output Validation',
              name: 'Name',
              type: 'Type',
              failure_behaviour: 'Failure Behaviour',
              field: 'Field',
              field_optional: 'Field (optional)',
              pattern: 'Pattern',
              schema_json: 'Schema (JSON)',
              rubric: 'Rubric',
              eval_name: 'Eval name',
              regex: 'Regex',
              json_schema: 'JSON Schema',
              llm_judge: 'LLM Judge',
              retry: 'Retry',
              block: 'Block',
              warn: 'Warn',
              regex_pattern: 'Regex pattern',
              output_field_name: 'Output field name',
            },
          },
        },
      },
    },
  },
})

interface EvalConfig {
  id: string
  name: string
  type: 'regex' | 'json_schema' | 'llm_judge'
  config: Record<string, unknown>
  failure_behaviour: 'retry' | 'block' | 'warn'
}

function makeEval(overrides: Partial<EvalConfig> = {}): EvalConfig {
  return {
    id: overrides.id ?? 'eval-1',
    name: overrides.name ?? 'Test Eval',
    type: overrides.type ?? 'regex',
    config: overrides.config ?? { field: '', pattern: '' },
    failure_behaviour: overrides.failure_behaviour ?? 'retry',
  }
}

describe('OutputValidationTab', () => {
  beforeEach(() => {
    vi.stubGlobal('crypto', { randomUUID: () => 'new-uuid-123' })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  it('shows the heading and eval count badge', () => {
    const evals = [makeEval({ id: 'e1' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 2 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Output Validation')
    expect(wrapper.text()).toContain('1 eval configured')
  })

  it('shows plural "evals" when count > 1', () => {
    const evals = [makeEval({ id: 'e1' }), makeEval({ id: 'e2' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('2 evals configured')
  })

  it('shows empty state when no evals', () => {
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: [], maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('No output validation evals configured.')
  })

  it('emits addEval via the add button', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: [], maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const addBtn = wrapper.findAll('button').find((b) => b.text().includes('Add Eval Definition'))
    expect(addBtn).toBeDefined()

    await addBtn!.trigger('click')

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toHaveLength(1)
    const newEvals = emitted[0][0] as EvalConfig[]
    expect(newEvals).toHaveLength(1)
    expect(newEvals[0].type).toBe('regex')
    expect(newEvals[0].failure_behaviour).toBe('retry')
  })

  it('emits removeEval when Remove is clicked', async () => {
    const evals = [makeEval({ id: 'e1' }), makeEval({ id: 'e2' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const removeBtns = wrapper.findAll('button').filter((b) => b.text().trim() === 'Remove')
    expect(removeBtns.length).toBe(2)

    await removeBtns[0].trigger('click')

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toHaveLength(1)
    const remaining = emitted[0][0] as EvalConfig[]
    expect(remaining).toHaveLength(1)
    expect(remaining[0].id).toBe('e2')
  })

  it('shows regex config fields when type is regex', () => {
    const evals = [makeEval({ id: 'e1', type: 'regex' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Field')
    expect(wrapper.text()).toContain('Pattern')
  })

  it('shows json_schema config fields when type is json_schema', () => {
    const evals = [makeEval({ id: 'e1', type: 'json_schema' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Field (optional)')
    expect(wrapper.text()).toContain('Schema (JSON)')
  })

  it('shows llm_judge config fields when type is llm_judge', () => {
    const evals = [makeEval({ id: 'e1', type: 'llm_judge' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Rubric')
  })

  it('does not show regex fields for json_schema type', () => {
    const evals = [makeEval({ id: 'e1', type: 'json_schema' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    // Pattern is a regex-only field
    expect(wrapper.text()).not.toContain('Regex pattern')
  })

  it('updates maxValidationRetries via range input', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: [], maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const rangeInput = wrapper.find('input[type="range"]')
    expect(rangeInput.exists()).toBe(true)

    await rangeInput.setValue(3)
    await rangeInput.trigger('input')

    expect(wrapper.emitted('update:maxValidationRetries')).toBeTruthy()
    const emitted = wrapper.emitted('update:maxValidationRetries') as unknown[][]
    expect(emitted[0][0]).toBe(3)
  })

  it('displays current localRetries value in the label', () => {
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: [], maxValidationRetries: 4 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Max Validation Retries: 4')
  })

  it('emits update:evalDefinitions when updating eval name', async () => {
    const evals = [makeEval({ id: 'e1', name: 'Old Name' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('New Name')
    await nameInput.trigger('update:modelValue')

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].name).toBe('New Name')
  })

  it('emits update:evalDefinitions when updating regex field', async () => {
    const evals = [makeEval({ id: 'e1', type: 'regex' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const fieldInput = wrapper.findAll('input[type="text"]')[1]
    await fieldInput.setValue('output_text')
    await fieldInput.trigger('update:modelValue')

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].config.field).toBe('output_text')
  })

  it('emits update:evalDefinitions when regex pattern textarea changes', async () => {
    const evals = [makeEval({ id: 'e1', type: 'regex' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const textarea = wrapper.find('textarea')
    await textarea.setValue('.*')
    await textarea.trigger('change')

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].config.pattern).toBe('.*')
  })

  it('emits update:evalDefinitions when json_schema schema textarea has valid JSON', async () => {
    const evals = [makeEval({ id: 'e1', type: 'json_schema', config: {} })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const textarea = wrapper.find('textarea')
    // Set value and dispatch change event manually since vue-test-utils
    // cannot pass event payload via trigger()
    const el = textarea.element as HTMLTextAreaElement
    el.value = '{"type":"object"}'
    el.dispatchEvent(new Event('change', { bubbles: true }))
    await wrapper.vm.$nextTick()

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].config.schema).toEqual({ type: 'object' })
  })

  it('does not emit on invalid JSON in json_schema textarea', async () => {
    const evals = [makeEval({ id: 'e1', type: 'json_schema', config: { schema: { type: 'object' } } })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const textarea = wrapper.find('textarea')
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const el = textarea.element as HTMLTextAreaElement
    el.value = '{invalid json'
    el.dispatchEvent(new Event('change', { bubbles: true }))
    await wrapper.vm.$nextTick()

    // Should not emit update:evalDefinitions for invalid JSON
    expect(wrapper.emitted('update:evalDefinitions')).toBeFalsy()
    expect(consoleSpy).toHaveBeenCalledWith('Invalid JSON schema, keeping current value')
    consoleSpy.mockRestore()
  })

  it('emits update:evalDefinitions when llm_judge rubric changes', async () => {
    const evals = [makeEval({ id: 'e1', type: 'llm_judge', config: { rubric: '' } })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const textarea = wrapper.find('textarea')
    await textarea.setValue('Be concise and accurate')
    await textarea.trigger('change')

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].config.rubric).toBe('Be concise and accurate')
  })

  it('renders Eval #N labels in order', () => {
    const evals = [
      makeEval({ id: 'e1' }),
      makeEval({ id: 'e2' }),
      makeEval({ id: 'e3' }),
    ]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Eval #1')
    expect(wrapper.text()).toContain('Eval #2')
    expect(wrapper.text()).toContain('Eval #3')
  })

  it('updates the Max Validation Retries label when the range input moves to its maximum', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: [], maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const rangeInput = wrapper.find('input[type="range"]')
    expect(wrapper.text()).toContain('Max Validation Retries: 0')

    // Range inputs coerce values to the nearest step; move to the max (5)
    await rangeInput.setValue(5)
    await rangeInput.trigger('input')

    // The label reflects the component's local reactive state, not just the emit
    expect(wrapper.text()).toContain('Max Validation Retries: 5')
  })

  it('handles empty eval definitions array without errors', () => {
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: [], maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('0 evals configured')
    expect(wrapper.find('button').text()).toContain('Add Eval Definition')
  })
})
