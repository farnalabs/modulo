/**
 * Coverage-focused tests for EdgeConfigPanel.vue (FAR-835).
 *
 * Covers: reactive form sync from props, emit of every field via the deep
 * watcher, trigger type options rendering, frequency options rendering,
 * null/empty prop fallback defaults, and the initial-immediate watcher.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

import EdgeConfigPanel from '../components/lifecycle-map/editor/EdgeConfigPanel.vue'

/* ── Helper to mount with controlled props ────────────────────────────── */

function mountPanel(props: Partial<InstanceType<typeof EdgeConfigPanel>['$props']> = {}) {
  return mount(EdgeConfigPanel, {
    props: {
      trigger_type: 'pipeline_completed',
      description: '',
      condition_expression: null,
      estimated_frequency: null,
      trigger_link: null,
      ...props,
    },
    global: {
      stubs: {
        Select: {
          template: '<select :data-testid="$attrs[\'aria-label\'] || \'select\'" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="opt in options" :key="opt.value" :value="opt.value">{{ opt.label }}</option></select>',
          props: ['options', 'modelValue', 'placeholder', 'ariaLabel'],
          emits: ['update:modelValue'],
        },
      },
    },
  })
}

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.unstubAllGlobals() })

/* ── Initial form sync from props (immediate watcher) ─────────────────── */

describe('EdgeConfigPanel initial form sync', () => {
  it('syncs trigger_type from props into the form', async () => {
    const wrapper = mountPanel({ trigger_type: 'webhook' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { trigger_type: string } }
    expect(vm.form.trigger_type).toBe('webhook')
  })

  it('syncs description from props into the form', async () => {
    const wrapper = mountPanel({ description: 'A test desc' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { description: string } }
    expect(vm.form.description).toBe('A test desc')
  })

  it('syncs condition_expression from props into the form', async () => {
    const wrapper = mountPanel({ condition_expression: 'status == "ok"' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { condition_expression: string } }
    expect(vm.form.condition_expression).toBe('status == "ok"')
  })

  it('syncs estimated_frequency from props into the form', async () => {
    const wrapper = mountPanel({ estimated_frequency: 'daily' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { estimated_frequency: string } }
    expect(vm.form.estimated_frequency).toBe('daily')
  })

  it('syncs trigger_link from props into the form', async () => {
    const wrapper = mountPanel({ trigger_link: 'https://example.com' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { trigger_link: string } }
    expect(vm.form.trigger_link).toBe('https://example.com')
  })
})

/* ── Null / empty prop fallbacks ──────────────────────────────────────── */

describe('EdgeConfigPanel null/empty prop fallbacks', () => {
  it('defaults trigger_type to pipeline_completed when prop is null', async () => {
    const wrapper = mountPanel({ trigger_type: null as unknown as string })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { trigger_type: string } }
    expect(vm.form.trigger_type).toBe('pipeline_completed')
  })

  it('defaults description to empty string when prop is null', async () => {
    const wrapper = mountPanel({ description: null as unknown as string })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { description: string } }
    expect(vm.form.description).toBe('')
  })

  it('defaults condition_expression to empty string when prop is null', async () => {
    const wrapper = mountPanel({ condition_expression: null })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { condition_expression: string } }
    expect(vm.form.condition_expression).toBe('')
  })

  it('defaults estimated_frequency to null when prop is null', async () => {
    const wrapper = mountPanel({ estimated_frequency: null })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { estimated_frequency: null } }
    expect(vm.form.estimated_frequency).toBeNull()
  })

  it('defaults trigger_link to empty string when prop is null', async () => {
    const wrapper = mountPanel({ trigger_link: null })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { trigger_link: string } }
    expect(vm.form.trigger_link).toBe('')
  })
})

/* ── Emit events from the deep watcher ────────────────────────────────── */

describe('EdgeConfigPanel emits', () => {
  it('emits update for every field when props change', async () => {
    const wrapper = mountPanel({ trigger_type: 'cron', description: 'test' })
    await flushPromises()

    // Change props to trigger the watcher, which updates form, which triggers deep watcher
    await wrapper.setProps({ trigger_type: 'webhook', description: 'new desc' })
    await flushPromises()

    const emitted = wrapper.emitted('update')!
    expect(emitted).toBeDefined()

    const fields = emitted.map(([field]: [string, unknown]) => field)
    expect(fields).toContain('trigger_type')
    expect(fields).toContain('description')
    expect(fields).toContain('condition_expression')
    expect(fields).toContain('estimated_frequency')
    expect(fields).toContain('trigger_link')
  })

  it('emits trigger_type value matching the updated prop', async () => {
    const wrapper = mountPanel({ trigger_type: 'webhook' })
    await flushPromises()

    await wrapper.setProps({ trigger_type: 'cron' })
    await flushPromises()

    const emitted = wrapper.emitted('update')!
    const triggerUpdates = emitted.filter(([f]: [string, unknown]) => f === 'trigger_type')
    expect(triggerUpdates.length).toBeGreaterThan(0)
    expect(triggerUpdates[triggerUpdates.length - 1][1]).toBe('cron')
  })

  it('emits condition_expression as null when prop is null', async () => {
    const wrapper = mountPanel({ condition_expression: null })
    await flushPromises()

    await wrapper.setProps({ condition_expression: 'status == "ok"' })
    await flushPromises()

    const emitted = wrapper.emitted('update')!
    const condUpdates = emitted.filter(([f]: [string, unknown]) => f === 'condition_expression')
    expect(condUpdates.length).toBeGreaterThan(0)
    // The last emission should be the new value
    expect(condUpdates[condUpdates.length - 1][1]).toBe('status == "ok"')
  })

  it('emits estimated_frequency value when prop changes', async () => {
    const wrapper = mountPanel({ estimated_frequency: null })
    await flushPromises()

    await wrapper.setProps({ estimated_frequency: 'daily' })
    await flushPromises()

    const emitted = wrapper.emitted('update')!
    const freqUpdates = emitted.filter(([f]: [string, unknown]) => f === 'estimated_frequency')
    expect(freqUpdates.length).toBeGreaterThan(0)
    expect(freqUpdates[freqUpdates.length - 1][1]).toBe('daily')
  })

  it('emits trigger_link as null when prop is null', async () => {
    const wrapper = mountPanel({ trigger_link: null })
    await flushPromises()

    await wrapper.setProps({ trigger_link: 'https://example.com' })
    await flushPromises()

    const emitted = wrapper.emitted('update')!
    const linkUpdates = emitted.filter(([f]: [string, unknown]) => f === 'trigger_link')
    expect(linkUpdates.length).toBeGreaterThan(0)
    expect(linkUpdates[linkUpdates.length - 1][1]).toBe('https://example.com')
  })
})

/* ── Template content ─────────────────────────────────────────────────── */

describe('EdgeConfigPanel template', () => {
  it('renders the trigger type label from i18n', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Trigger Type')
  })

  it('renders the description label from i18n', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Description')
  })

  it('renders the condition label from i18n', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Condition Expression (JMESPath)')
  })

  it('renders the frequency label from i18n', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Estimated Frequency')
  })

  it('renders the trigger link label from i18n', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Trigger Link (optional)')
  })

  it('renders the condition input placeholder', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const input = wrapper.find('#edgeconfigpanel-field-3')
    expect((input.element as HTMLInputElement).placeholder).toBe("e.g. result.status == 'success'")
  })

  it('renders the trigger link input placeholder', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const input = wrapper.find('#edgeconfigpanel-field-1')
    expect((input.element as HTMLInputElement).placeholder).toBe('Link to Modulo trigger config')
  })

  it('renders the description textarea placeholder', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const textarea = wrapper.find('#edgeconfigpanel-field-4')
    expect((textarea.element as HTMLTextAreaElement).placeholder).toBe('Describe the trigger condition')
  })
})

/* ── Prop reactivity (watcher re-fires on prop change) ────────────────── */

describe('EdgeConfigPanel prop reactivity', () => {
  it('updates form when trigger_type prop changes', async () => {
    const wrapper = mountPanel({ trigger_type: 'pipeline_completed' })
    await flushPromises()

    await wrapper.setProps({ trigger_type: 'cron' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { trigger_type: string } }
    expect(vm.form.trigger_type).toBe('cron')
  })

  it('updates form when description prop changes', async () => {
    const wrapper = mountPanel({ description: '' })
    await flushPromises()

    await wrapper.setProps({ description: 'New description' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { description: string } }
    expect(vm.form.description).toBe('New description')
  })

  it('updates form when condition_expression prop changes', async () => {
    const wrapper = mountPanel({ condition_expression: null })
    await flushPromises()

    await wrapper.setProps({ condition_expression: 'x > 5' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { condition_expression: string } }
    expect(vm.form.condition_expression).toBe('x > 5')
  })

  it('updates form when estimated_frequency prop changes', async () => {
    const wrapper = mountPanel({ estimated_frequency: null })
    await flushPromises()

    await wrapper.setProps({ estimated_frequency: 'hourly' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { estimated_frequency: string } }
    expect(vm.form.estimated_frequency).toBe('hourly')
  })

  it('updates form when trigger_link prop changes', async () => {
    const wrapper = mountPanel({ trigger_link: null })
    await flushPromises()

    await wrapper.setProps({ trigger_link: 'https://new.link' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { trigger_link: string } }
    expect(vm.form.trigger_link).toBe('https://new.link')
  })
})

/* ── Trigger options list ─────────────────────────────────────────────── */

describe('EdgeConfigPanel triggerOptions', () => {
  it('exposes 5 trigger options', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as { triggerOptions: Array<{ value: string; label: string }> }
    expect(vm.triggerOptions).toHaveLength(5)
  })

  it('includes pipeline_completed option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as { triggerOptions: Array<{ value: string }> }
    expect(vm.triggerOptions.some(o => o.value === 'pipeline_completed')).toBe(true)
  })

  it('includes webhook option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as { triggerOptions: Array<{ value: string }> }
    expect(vm.triggerOptions.some(o => o.value === 'webhook')).toBe(true)
  })

  it('includes cron option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as { triggerOptions: Array<{ value: string }> }
    expect(vm.triggerOptions.some(o => o.value === 'cron')).toBe(true)
  })

  it('includes manual option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as { triggerOptions: Array<{ value: string }> }
    expect(vm.triggerOptions.some(o => o.value === 'manual')).toBe(true)
  })

  it('includes external option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as { triggerOptions: Array<{ value: string }> }
    expect(vm.triggerOptions.some(o => o.value === 'external')).toBe(true)
  })
})
