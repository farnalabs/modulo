/**
 * Coverage-focused tests for StageConfigPanel.vue (FAR-835).
 *
 * Covers: reactive form sync from props, emit of every field, stage type
 * button rendering & selection, conditional pipeline/external_url panels,
 * isGraduatable computed (modulo, external, manual, placeholder × graduated),
 * graduate emit, and prop reactivity.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

import StageConfigPanel from '../components/lifecycle-map/editor/StageConfigPanel.vue'

const pipelines = [
  { id: 'pipe-1', name: 'Deploy Pipeline', visibility: 'org', created_at: '2026-01-01T00:00:00Z' },
  { id: 'pipe-2', name: 'Build Pipeline', visibility: 'org', created_at: '2026-01-02T00:00:00Z' },
]

function mountPanel(props: Partial<InstanceType<typeof StageConfigPanel>['$props']> = {}) {
  return mount(StageConfigPanel, {
    props: {
      stageId: 'stage-1',
      name: '',
      description: '',
      stage_type: 'placeholder',
      pipeline_id: null,
      external_url: null,
      owner: null,
      graduated: false,
      pipelines,
      ...props,
    },
    global: {
      stubs: {
        InputText: {
          template: '<input :data-testid="\'input-\' + ($attrs.placeholder || \'default\')" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
          props: ['modelValue'],
          emits: ['update:modelValue'],
        },
        Button: {
          template: '<button :disabled="disabled" data-testid="button" @click="$emit(\'click\', $event)"><slot /></button>',
          props: ['severity', 'outlined', 'disabled'],
          emits: ['click'],
        },
        Select: {
          template: '<select :data-testid="\'select-\' + (ariaLabel || \'default\')" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="opt in options" :key="opt.value" :value="opt.value">{{ opt.label }}</option></select>',
          props: ['options', 'modelValue', 'placeholder', 'ariaLabel'],
          emits: ['update:modelValue'],
        },
      },
    },
  })
}

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.unstubAllGlobals() })

/* ── Initial form sync from props ─────────────────────────────────────── */

describe('StageConfigPanel initial form sync', () => {
  it('syncs name from props', async () => {
    const wrapper = mountPanel({ name: 'Build' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { name: string } }
    expect(vm.form.name).toBe('Build')
  })

  it('syncs description from props', async () => {
    const wrapper = mountPanel({ description: 'Test desc' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { description: string } }
    expect(vm.form.description).toBe('Test desc')
  })

  it('syncs stage_type from props', async () => {
    const wrapper = mountPanel({ stage_type: 'modulo' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { stage_type: string } }
    expect(vm.form.stage_type).toBe('modulo')
  })

  it('syncs pipeline_id from props', async () => {
    const wrapper = mountPanel({ pipeline_id: 'pipe-1' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { pipeline_id: string } }
    expect(vm.form.pipeline_id).toBe('pipe-1')
  })

  it('syncs external_url from props', async () => {
    const wrapper = mountPanel({ external_url: 'https://example.com' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { external_url: string } }
    expect(vm.form.external_url).toBe('https://example.com')
  })

  it('syncs owner from props', async () => {
    const wrapper = mountPanel({ owner: 'team-alpha' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { owner: string } }
    expect(vm.form.owner).toBe('team-alpha')
  })
})

/* ── Null prop fallbacks ──────────────────────────────────────────────── */

describe('StageConfigPanel null prop fallbacks', () => {
  it('defaults name to empty string when null', async () => {
    const wrapper = mountPanel({ name: null as unknown as string })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { name: string } }
    expect(vm.form.name).toBe('')
  })

  it('defaults description to empty string when null', async () => {
    const wrapper = mountPanel({ description: null as unknown as string })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { description: string } }
    expect(vm.form.description).toBe('')
  })

  it('defaults stage_type to placeholder when null', async () => {
    const wrapper = mountPanel({ stage_type: null as unknown as string })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { stage_type: string } }
    expect(vm.form.stage_type).toBe('placeholder')
  })

  it('defaults pipeline_id to null when null', async () => {
    const wrapper = mountPanel({ pipeline_id: null })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { pipeline_id: null } }
    expect(vm.form.pipeline_id).toBeNull()
  })

  it('defaults external_url to empty string when null', async () => {
    const wrapper = mountPanel({ external_url: null })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { external_url: string } }
    expect(vm.form.external_url).toBe('')
  })

  it('defaults owner to empty string when null', async () => {
    const wrapper = mountPanel({ owner: null })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { owner: string } }
    expect(vm.form.owner).toBe('')
  })
})

/* ── Emit events from deep watcher ────────────────────────────────────── */

describe('StageConfigPanel emits', () => {
  it('emits update for every field when props change', async () => {
    const wrapper = mountPanel({ name: 'Build', stage_type: 'modulo' })
    await flushPromises()

    // Change props to trigger the watcher → form update → deep watcher → emit
    await wrapper.setProps({ name: 'Deploy', stage_type: 'external' })
    await flushPromises()

    const emitted = wrapper.emitted('update')!
    expect(emitted).toBeDefined()

    const fields = emitted.map(([field]: [string, unknown]) => field)
    expect(fields).toContain('name')
    expect(fields).toContain('description')
    expect(fields).toContain('stage_type')
    expect(fields).toContain('pipeline_id')
    expect(fields).toContain('external_url')
    expect(fields).toContain('owner')
  })

  it('emits stage_type value when prop changes', async () => {
    const wrapper = mountPanel({ stage_type: 'modulo' })
    await flushPromises()

    await wrapper.setProps({ stage_type: 'external' })
    await flushPromises()

    const emitted = wrapper.emitted('update')!
    const typeUpdates = emitted.filter(([f]: [string, unknown]) => f === 'stage_type')
    expect(typeUpdates.length).toBeGreaterThan(0)
    expect(typeUpdates[typeUpdates.length - 1][1]).toBe('external')
  })

  it('emits external_url with value when prop changes', async () => {
    const wrapper = mountPanel({ external_url: null })
    await flushPromises()

    await wrapper.setProps({ external_url: 'https://example.com' })
    await flushPromises()

    const emitted = wrapper.emitted('update')!
    const urlUpdates = emitted.filter(([f]: [string, unknown]) => f === 'external_url')
    expect(urlUpdates.length).toBeGreaterThan(0)
    expect(urlUpdates[urlUpdates.length - 1][1]).toBe('https://example.com')
  })

  it('emits owner with value when prop changes', async () => {
    const wrapper = mountPanel({ owner: null })
    await flushPromises()

    await wrapper.setProps({ owner: 'team-beta' })
    await flushPromises()

    const emitted = wrapper.emitted('update')!
    const ownerUpdates = emitted.filter(([f]: [string, unknown]) => f === 'owner')
    expect(ownerUpdates.length).toBeGreaterThan(0)
    expect(ownerUpdates[ownerUpdates.length - 1][1]).toBe('team-beta')
  })
})

/* ── Stage type buttons ───────────────────────────────────────────────── */

describe('StageConfigPanel stage type buttons', () => {
  it('renders 4 stage type buttons', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const buttons = wrapper.findAll('button[type="button"]')
    // 4 stage type buttons + 0 graduate button (placeholder type, not graduatable)
    expect(buttons.length).toBeGreaterThanOrEqual(4)
  })

  it('includes Modulo option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Modulo')
  })

  it('includes External option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('External')
  })

  it('includes Manual option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Manual')
  })

  it('includes Placeholder option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Placeholder')
  })

  it('shows description for each stage type option', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Managed by a Modulo pipeline')
    expect(wrapper.text()).toContain('Runs outside Modulo')
    expect(wrapper.text()).toContain('Human-performed step')
    expect(wrapper.text()).toContain('Not yet defined')
  })
})

/* ── Stage type selection changes form ────────────────────────────────── */

describe('StageConfigPanel stage type selection', () => {
  it('changes stage_type when a button is clicked', async () => {
    const wrapper = mountPanel({ stage_type: 'placeholder' })
    await flushPromises()

    // Find and click the Modulo button
    const buttons = wrapper.findAll('button[type="button"]')
    const moduloBtn = buttons.find(b => b.text().includes('Modulo'))!
    await moduloBtn.trigger('click')
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { stage_type: string } }
    expect(vm.form.stage_type).toBe('modulo')
  })

  it('can switch to external type', async () => {
    const wrapper = mountPanel({ stage_type: 'placeholder' })
    await flushPromises()

    const buttons = wrapper.findAll('button[type="button"]')
    const extBtn = buttons.find(b => b.text().includes('External'))!
    await extBtn.trigger('click')
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { stage_type: string } }
    expect(vm.form.stage_type).toBe('external')
  })

  it('can switch to manual type', async () => {
    const wrapper = mountPanel({ stage_type: 'placeholder' })
    await flushPromises()

    const buttons = wrapper.findAll('button[type="button"]')
    const manualBtn = buttons.find(b => b.text().includes('Manual'))!
    await manualBtn.trigger('click')
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { stage_type: string } }
    expect(vm.form.stage_type).toBe('manual')
  })
})

/* ── Conditional pipeline select (modulo) ─────────────────────────────── */

describe('StageConfigPanel pipeline select', () => {
  it('shows pipeline select when stage_type is modulo', async () => {
    const wrapper = mountPanel({ stage_type: 'modulo' })
    await flushPromises()
    expect(wrapper.find('[data-testid="select-Pipeline"]').exists()).toBe(true)
  })

  it('hides pipeline select when stage_type is not modulo', async () => {
    const wrapper = mountPanel({ stage_type: 'external' })
    await flushPromises()
    expect(wrapper.find('[data-testid="select-Pipeline"]').exists()).toBe(false)
  })

  it('hides pipeline select when stage_type is manual', async () => {
    const wrapper = mountPanel({ stage_type: 'manual' })
    await flushPromises()
    expect(wrapper.find('[data-testid="select-Pipeline"]').exists()).toBe(false)
  })

  it('hides pipeline select when stage_type is placeholder', async () => {
    const wrapper = mountPanel({ stage_type: 'placeholder' })
    await flushPromises()
    expect(wrapper.find('[data-testid="select-Pipeline"]').exists()).toBe(false)
  })

  it('passes pipelines as options', async () => {
    const wrapper = mountPanel({ stage_type: 'modulo' })
    await flushPromises()
    const select = wrapper.find('[data-testid="select-Pipeline"]')
    const options = select.findAll('option')
    expect(options).toHaveLength(2)
    expect(options[0].text()).toBe('Deploy Pipeline')
    expect(options[1].text()).toBe('Build Pipeline')
  })
})

/* ── Conditional external_url (external) ──────────────────────────────── */

describe('StageConfigPanel external_url', () => {
  it('shows external_url input when stage_type is external', async () => {
    const wrapper = mountPanel({ stage_type: 'external' })
    await flushPromises()
    expect(wrapper.text()).toContain('External URL')
  })

  it('hides external_url input when stage_type is not external', async () => {
    const wrapper = mountPanel({ stage_type: 'modulo' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('External URL')
  })

  it('hides external_url when manual', async () => {
    const wrapper = mountPanel({ stage_type: 'manual' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('External URL')
  })
})

/* ── isGraduatable computed ───────────────────────────────────────────── */

describe('StageConfigPanel isGraduatable', () => {
  it('is true for manual type when not graduated', async () => {
    const wrapper = mountPanel({ stage_type: 'manual', graduated: false })
    await flushPromises()
    const vm = wrapper.vm as unknown as { isGraduatable: boolean }
    expect(vm.isGraduatable).toBe(true)
  })

  it('is true for external type when not graduated', async () => {
    const wrapper = mountPanel({ stage_type: 'external', graduated: false })
    await flushPromises()
    const vm = wrapper.vm as unknown as { isGraduatable: boolean }
    expect(vm.isGraduatable).toBe(true)
  })

  it('is false for modulo type when not graduated', async () => {
    const wrapper = mountPanel({ stage_type: 'modulo', graduated: false })
    await flushPromises()
    const vm = wrapper.vm as unknown as { isGraduatable: boolean }
    expect(vm.isGraduatable).toBe(false)
  })

  it('is false for placeholder type when not graduated', async () => {
    const wrapper = mountPanel({ stage_type: 'placeholder', graduated: false })
    await flushPromises()
    const vm = wrapper.vm as unknown as { isGraduatable: boolean }
    expect(vm.isGraduatable).toBe(false)
  })

  it('is false for manual type when already graduated', async () => {
    const wrapper = mountPanel({ stage_type: 'manual', graduated: true })
    await flushPromises()
    const vm = wrapper.vm as unknown as { isGraduatable: boolean }
    expect(vm.isGraduatable).toBe(false)
  })

  it('is false for external type when already graduated', async () => {
    const wrapper = mountPanel({ stage_type: 'external', graduated: true })
    await flushPromises()
    const vm = wrapper.vm as unknown as { isGraduatable: boolean }
    expect(vm.isGraduatable).toBe(false)
  })
})

/* ── Graduate button ──────────────────────────────────────────────────── */

describe('StageConfigPanel graduate button', () => {
  it('shows graduate button for manual type when not graduated', async () => {
    const wrapper = mountPanel({ stage_type: 'manual', graduated: false })
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text().includes('Graduate Stage'))
    expect(gradBtn).toBeDefined()
  })

  it('shows graduate button for external type when not graduated', async () => {
    const wrapper = mountPanel({ stage_type: 'external', graduated: false })
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text().includes('Graduate Stage'))
    expect(gradBtn).toBeDefined()
  })

  it('hides graduate button for modulo type', async () => {
    const wrapper = mountPanel({ stage_type: 'modulo', graduated: false })
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text().includes('Graduate Stage'))
    expect(gradBtn).toBeUndefined()
  })

  it('hides graduate button for placeholder type', async () => {
    const wrapper = mountPanel({ stage_type: 'placeholder', graduated: false })
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text().includes('Graduate Stage'))
    expect(gradBtn).toBeUndefined()
  })

  it('hides graduate button when already graduated', async () => {
    const wrapper = mountPanel({ stage_type: 'manual', graduated: true })
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text().includes('Graduate Stage'))
    expect(gradBtn).toBeUndefined()
  })

  it('emits graduate with form data when clicked', async () => {
    const wrapper = mountPanel({ stageId: 'stage-42', name: 'My Stage', stage_type: 'manual' })
    await flushPromises()

    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text().includes('Graduate Stage'))!
    await gradBtn.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('graduate')).toBeDefined()
    const emittedData = wrapper.emitted('graduate')![0][0] as { id: string; name: string; stage_type: string }
    expect(emittedData.id).toBe('stage-42')
    expect(emittedData.name).toBe('My Stage')
    expect(emittedData.stage_type).toBe('manual')
  })

  it('shows "Promote this" description text for graduatable stage', async () => {
    const wrapper = mountPanel({ stage_type: 'manual', graduated: false })
    await flushPromises()
    expect(wrapper.text()).toContain('Promote this')
    expect(wrapper.text()).toContain('stage to a Modulo-managed pipeline')
  })
})

/* ── Prop reactivity ──────────────────────────────────────────────────── */

describe('StageConfigPanel prop reactivity', () => {
  it('updates form when name prop changes', async () => {
    const wrapper = mountPanel({ name: '' })
    await flushPromises()

    await wrapper.setProps({ name: 'New Name' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { name: string } }
    expect(vm.form.name).toBe('New Name')
  })

  it('updates form when stage_type prop changes', async () => {
    const wrapper = mountPanel({ stage_type: 'placeholder' })
    await flushPromises()

    await wrapper.setProps({ stage_type: 'modulo' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { stage_type: string } }
    expect(vm.form.stage_type).toBe('modulo')
  })

  it('updates form when pipeline_id prop changes', async () => {
    const wrapper = mountPanel({ stage_type: 'modulo', pipeline_id: null })
    await flushPromises()

    await wrapper.setProps({ pipeline_id: 'pipe-2' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { pipeline_id: string } }
    expect(vm.form.pipeline_id).toBe('pipe-2')
  })

  it('updates form when external_url prop changes', async () => {
    const wrapper = mountPanel({ stage_type: 'external', external_url: null })
    await flushPromises()

    await wrapper.setProps({ external_url: 'https://new.url' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { external_url: string } }
    expect(vm.form.external_url).toBe('https://new.url')
  })

  it('updates form when owner prop changes', async () => {
    const wrapper = mountPanel({ owner: null })
    await flushPromises()

    await wrapper.setProps({ owner: 'new-team' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { form: { owner: string } }
    expect(vm.form.owner).toBe('new-team')
  })
})

/* ── Template labels ──────────────────────────────────────────────────── */

describe('StageConfigPanel template labels', () => {
  it('renders Name label', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Name')
  })

  it('renders Description label', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Description')
  })

  it('renders Type label', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Type')
  })

  it('renders Owner label', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Owner')
  })

  it('renders name input placeholder', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const input = wrapper.find('[data-testid="input-Stage name"]')
    expect(input.exists()).toBe(true)
  })

  it('renders owner input placeholder', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const input = wrapper.find('[data-testid="input-Team or person name"]')
    expect(input.exists()).toBe(true)
  })
})

/* ── stageTypeOptions ─────────────────────────────────────────────────── */

describe('StageConfigPanel stageTypeOptions', () => {
  it('exposes 4 options', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as { stageTypeOptions: Array<{ value: string }> }
    expect(vm.stageTypeOptions).toHaveLength(4)
  })

  it('has modulo, external, manual, placeholder values', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as { stageTypeOptions: Array<{ value: string }> }
    const values = vm.stageTypeOptions.map(o => o.value)
    expect(values).toContain('modulo')
    expect(values).toContain('external')
    expect(values).toContain('manual')
    expect(values).toContain('placeholder')
  })
})
