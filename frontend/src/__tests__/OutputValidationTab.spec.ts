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
              model_judge: 'Model judge',
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
  type: 'regex' | 'json_schema' | 'model_judge'
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

  it('shows model_judge config fields when type is model_judge', () => {
    const evals = [makeEval({ id: 'e1', type: 'model_judge' })]
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

  it('emits update:evalDefinitions when model_judge rubric changes', async () => {
    const evals = [makeEval({ id: 'e1', type: 'model_judge', config: { rubric: '' } })]
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

  it('emits update:evalDefinitions when type select changes', async () => {
    const evals = [makeEval({ id: 'e1', type: 'regex' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    ;(wrapper.vm as unknown as { updateEval: (id: string, patch: Partial<EvalConfig>) => void }).updateEval('e1', { type: 'json_schema' })

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].type).toBe('json_schema')
    // Config should be reset for new type
    expect(updated[0].config).toEqual({ field: '', pattern: '' })
  })

  it('preserves config when type changes from regex to model_judge', async () => {
    const evals = [makeEval({ id: 'e1', type: 'regex', config: { field: 'output', pattern: '.*' } })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    ;(wrapper.vm as unknown as { updateEval: (id: string, patch: Partial<EvalConfig>) => void }).updateEval('e1', { type: 'model_judge' })
    await wrapper.vm.$nextTick()

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].type).toBe('model_judge')
    // Config is NOT reset by updateEval - it preserves existing config
    // Only addEval creates a new eval with default config
    expect(updated[0].config).toEqual({ field: 'output', pattern: '.*' })
  })

  it('emits update:evalDefinitions when failure_behaviour select changes', async () => {
    const evals = [makeEval({ id: 'e1', failure_behaviour: 'retry' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    ;(wrapper.vm as unknown as { updateEval: (id: string, patch: Partial<EvalConfig>) => void }).updateEval('e1', { failure_behaviour: 'block' })

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].failure_behaviour).toBe('block')
  })

  it('emits update:evalDefinitions when updating json_schema field_optional', async () => {
    const evals = [makeEval({ id: 'e1', type: 'json_schema', config: { field: '' } })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const fieldInput = wrapper.find('input[placeholder="output field name (leave blank for entire output)"]')
    await fieldInput.setValue('data.result')
    await fieldInput.trigger('update:modelValue')

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].config.field).toBe('data.result')
  })

  it('shows regex fields when type changes to regex', async () => {
    const evals = [makeEval({ id: 'e1', type: 'json_schema' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).not.toContain('Regex pattern')

    ;(wrapper.vm as unknown as { updateEval: (id: string, patch: Partial<EvalConfig>) => void }).updateEval('e1', { type: 'regex' })
    await wrapper.vm.$nextTick()

    // Update the prop to simulate parent handling the update:evalDefinitions event
    const updatedEvals: EvalConfig[] = [{ ...evals[0], type: 'regex' }]
    await wrapper.setProps({ evalDefinitions: updatedEvals })

    expect(wrapper.text()).toContain('Field')
    expect(wrapper.text()).toContain('Pattern')
    // The regex pattern textarea placeholder should be present
    const textarea = wrapper.find('textarea')
    expect(textarea.attributes('placeholder')).toContain('Regex pattern')
  })

  it('shows model_judge fields when type changes to model_judge', async () => {
    const evals = [makeEval({ id: 'e1', type: 'regex' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).not.toContain('Rubric')

    ;(wrapper.vm as unknown as { updateEval: (id: string, patch: Partial<EvalConfig>) => void }).updateEval('e1', { type: 'model_judge' })
    await wrapper.vm.$nextTick()

    // Update the prop to simulate parent handling the update:evalDefinitions event
    const updatedEvals: EvalConfig[] = [{ ...evals[0], type: 'model_judge' }]
    await wrapper.setProps({ evalDefinitions: updatedEvals })

    expect(wrapper.text()).toContain('Rubric')
  })

  it('emits update:evalDefinitions when updating failure_behaviour to warn', async () => {
    const evals = [makeEval({ id: 'e1', failure_behaviour: 'retry' })]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    ;(wrapper.vm as unknown as { updateEval: (id: string, patch: Partial<EvalConfig>) => void }).updateEval('e1', { failure_behaviour: 'warn' })

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    expect(emitted).toBeTruthy()
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].failure_behaviour).toBe('warn')
  })

  it('handles multiple eval definitions independently', async () => {
    const evals = [
      makeEval({ id: 'e1', type: 'regex', name: 'Eval 1' }),
      makeEval({ id: 'e2', type: 'json_schema', name: 'Eval 2' }),
    ]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Eval #1')
    expect(wrapper.text()).toContain('Eval #2')

    // Update first eval
    const nameInputs = wrapper.findAll('input[type="text"]')
    await nameInputs[0].setValue('Updated Eval 1')
    await nameInputs[0].trigger('update:modelValue')

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    const updated = emitted[0][0] as EvalConfig[]
    expect(updated[0].name).toBe('Updated Eval 1')
    expect(updated[1].name).toBe('Eval 2')
  })

  it('removes correct eval when multiple exist', async () => {
    const evals = [
      makeEval({ id: 'e1', name: 'Eval 1' }),
      makeEval({ id: 'e2', name: 'Eval 2' }),
      makeEval({ id: 'e3', name: 'Eval 3' }),
    ]
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: evals, maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const removeBtns = wrapper.findAll('button').filter((b) => b.text().trim() === 'Remove')
    expect(removeBtns.length).toBe(3)

    await removeBtns[1].trigger('click') // Remove middle one

    const emitted = wrapper.emitted('update:evalDefinitions') as unknown[][]
    const remaining = emitted[0][0] as EvalConfig[]
    expect(remaining).toHaveLength(2)
    expect(remaining[0].id).toBe('e1')
    expect(remaining[1].id).toBe('e3')
  })

  it('localRetries is initialized from prop but does not react to prop changes', () => {
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: [], maxValidationRetries: 1 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Max Validation Retries: 1')

    wrapper.setProps({ maxValidationRetries: 3 })
    // localRetries is a local ref initialized from prop, not a computed that watches prop
    // So it won't update when prop changes
    expect(wrapper.text()).toContain('Max Validation Retries: 1')
  })

  it('range input min/max bounds', () => {
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: [], maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    const rangeInput = wrapper.find('input[type="range"]')
    expect(rangeInput.attributes('min')).toBe('0')
    expect(rangeInput.attributes('max')).toBe('5')
    expect(rangeInput.attributes('step')).toBe('1')
  })

  it('evalCount computed reflects current evalDefinitions length', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: { evalDefinitions: [makeEval({ id: 'e1' }), makeEval({ id: 'e2' })], maxValidationRetries: 0 },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('2 evals configured')

    await wrapper.setProps({ evalDefinitions: [makeEval({ id: 'e1' })] })
    expect(wrapper.text()).toContain('1 eval configured')
  })
})
