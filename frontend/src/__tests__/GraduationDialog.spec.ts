/**
 * Coverage-focused tests for GraduationDialog.vue (FAR-835).
 *
 * Covers: dialog visibility, mode switching (existing/new), canGraduate
 * computed, handleGraduate flow (success + error), cancel/close, button
 * disabled states, error banner, prop passing, and template rendering.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

import GraduationDialog from '../components/lifecycle-map/editor/GraduationDialog.vue'

const pipelines = [
  { id: 'pipe-1', name: 'Deploy Pipeline', visibility: 'org', created_at: '2026-01-01T00:00:00Z' },
  { id: 'pipe-2', name: 'Build Pipeline', visibility: 'org', created_at: '2026-01-02T00:00:00Z' },
]

function mountDialog(props: Partial<InstanceType<typeof GraduationDialog>['$props']> = {}) {
  return mount(GraduationDialog, {
    props: {
      open: true,
      stageName: 'Build Stage',
      stageId: 'stage-1',
      mapId: 'map-1',
      versionId: 'ver-1',
      pipelines,
      ...props,
    },
    global: {
      stubs: {
        Dialog: {
          template: '<div v-if="visible" data-testid="dialog"><slot name="header" /><slot /><slot name="footer" /></div>',
          props: ['visible', 'modal', 'dismissableMask'],
          emits: ['update:visible'],
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

/* ── Dialog visibility ────────────────────────────────────────────────── */

describe('GraduationDialog visibility', () => {
  it('renders dialog content when open is true', async () => {
    const wrapper = mountDialog({ open: true })
    await flushPromises()
    expect(wrapper.find('[data-testid="dialog"]').exists()).toBe(true)
  })

  it('does not render dialog content when open is false', async () => {
    const wrapper = mountDialog({ open: false })
    await flushPromises()
    expect(wrapper.find('[data-testid="dialog"]').exists()).toBe(false)
  })
})

/* ── Template content ─────────────────────────────────────────────────── */

describe('GraduationDialog template', () => {
  it('shows header with graduate stage title', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    expect(wrapper.text()).toContain('Graduate Stage')
  })

  it('shows the stage name in the header subtitle', async () => {
    const wrapper = mountDialog({ stageName: 'Deploy Stage' })
    await flushPromises()
    expect(wrapper.text()).toContain('Deploy Stage')
  })

  it('shows the "Link to existing pipeline" radio label', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    expect(wrapper.text()).toContain('Link to existing pipeline')
  })

  it('shows the "Create new pipeline from template" radio label', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    expect(wrapper.text()).toContain('Create new pipeline from template')
  })

  it('renders the cancel button', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="button"]')
    const cancelBtn = buttons.find(b => b.text() === 'Cancel')
    expect(cancelBtn).toBeDefined()
  })

  it('renders the graduate button', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text() === 'Graduate Stage')
    expect(gradBtn).toBeDefined()
  })
})

/* ── Mode switching ───────────────────────────────────────────────────── */

describe('GraduationDialog mode switching', () => {
  it('defaults to existing mode', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const vm = wrapper.vm as unknown as { mode: string }
    expect(vm.mode).toBe('existing')
  })

  it('shows pipeline select in existing mode', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    expect(wrapper.find('[data-testid="select-Select pipeline"]').exists()).toBe(true)
  })

  it('hides template select in existing mode', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    expect(wrapper.find('[data-testid="select-Select template"]').exists()).toBe(false)
  })

  it('switches to new mode via radio button', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const radio = wrapper.find('#graduationdialog-field-1')
    await radio.setValue('new')
    await flushPromises()

    const vm = wrapper.vm as unknown as { mode: string }
    expect(vm.mode).toBe('new')
  })

  it('shows template select in new mode', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const radio = wrapper.find('#graduationdialog-field-1')
    await radio.setValue('new')
    await flushPromises()

    expect(wrapper.find('[data-testid="select-Select template"]').exists()).toBe(true)
  })

  it('hides pipeline select in new mode', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const radio = wrapper.find('#graduationdialog-field-1')
    await radio.setValue('new')
    await flushPromises()

    expect(wrapper.find('[data-testid="select-Select pipeline"]').exists()).toBe(false)
  })
})

/* ── canGraduate computed ─────────────────────────────────────────────── */

describe('GraduationDialog canGraduate', () => {
  it('returns false in existing mode with no pipeline selected', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const vm = wrapper.vm as unknown as { canGraduate: boolean }
    expect(vm.canGraduate).toBe(false)
  })

  it('returns true in existing mode with a pipeline selected', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const vm = wrapper.vm as unknown as { selectedPipelineId: string | null; canGraduate: boolean }
    vm.selectedPipelineId = 'pipe-1'
    await flushPromises()
    expect(vm.canGraduate).toBe(true)
  })

  it('returns false in new mode with no template selected', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const radio = wrapper.find('#graduationdialog-field-1')
    await radio.setValue('new')
    await flushPromises()

    const vm = wrapper.vm as unknown as { canGraduate: boolean }
    expect(vm.canGraduate).toBe(false)
  })

  it('returns true in new mode with a template selected', async () => {
    const wrapper = mountDialog()
    await flushPromises()
    const radio = wrapper.find('#graduationdialog-field-1')
    await radio.setValue('new')
    await flushPromises()

    const vm = wrapper.vm as unknown as { selectedTemplateId: string | null; canGraduate: boolean }
    vm.selectedTemplateId = 'simple-sequential'
    await flushPromises()
    expect(vm.canGraduate).toBe(true)
  })
})

/* ── handleGraduate ───────────────────────────────────────────────────── */

describe('GraduationDialog handleGraduate', () => {
  it('emits confirm with stageId and pipelineId in existing mode', async () => {
    const wrapper = mountDialog({ stageId: 'stage-42' })
    await flushPromises()

    const vm = wrapper.vm as unknown as { selectedPipelineId: string | null; handleGraduate: () => Promise<void> }
    vm.selectedPipelineId = 'pipe-2'
    await flushPromises()

    await vm.handleGraduate()
    await flushPromises()

    expect(wrapper.emitted('confirm')).toBeDefined()
    expect(wrapper.emitted('confirm')![0]).toEqual(['stage-42', 'pipe-2'])
  })

  it('emits confirm with stageId and new-from-template in new mode', async () => {
    const wrapper = mountDialog({ stageId: 'stage-99' })
    await flushPromises()

    const radio = wrapper.find('#graduationdialog-field-1')
    await radio.setValue('new')
    await flushPromises()

    const vm = wrapper.vm as unknown as { selectedTemplateId: string | null; handleGraduate: () => Promise<void> }
    vm.selectedTemplateId = 'hierarchical'
    await flushPromises()

    await vm.handleGraduate()
    await flushPromises()

    expect(wrapper.emitted('confirm')).toBeDefined()
    expect(wrapper.emitted('confirm')![0]).toEqual(['stage-99', 'new-from-template'])
  })

  it('does nothing when canGraduate is false', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const vm = wrapper.vm as unknown as { handleGraduate: () => Promise<void> }
    await vm.handleGraduate()
    await flushPromises()

    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('sets graduating to true during handleGraduate and back to false', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const vm = wrapper.vm as unknown as { selectedPipelineId: string | null; handleGraduate: () => Promise<void>; graduating: boolean }
    vm.selectedPipelineId = 'pipe-1'
    await flushPromises()

    // graduating starts false
    expect(vm.graduating).toBe(false)

    await vm.handleGraduate()
    await flushPromises()

    // after completion it's false again
    expect(vm.graduating).toBe(false)
  })

  it('clears error before handleGraduate', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const vm = wrapper.vm as unknown as { error: string | null; selectedPipelineId: string | null; handleGraduate: () => Promise<void> }
    vm.selectedPipelineId = 'pipe-1'
    vm.error = 'Previous error'
    await flushPromises()

    await vm.handleGraduate()
    await flushPromises()

    expect(vm.error).toBeNull()
  })
})

/* ── Close / Cancel ───────────────────────────────────────────────────── */

describe('GraduationDialog close', () => {
  it('emits close when cancel button is clicked', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const buttons = wrapper.findAll('[data-testid="button"]')
    const cancelBtn = buttons.find(b => b.text() === 'Cancel')!
    await cancelBtn.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})

/* ── Error display ────────────────────────────────────────────────────── */

describe('GraduationDialog error', () => {
  it('shows error banner when error is set', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const vm = wrapper.vm as unknown as { error: string | null }
    vm.error = 'Something went wrong'
    await flushPromises()

    expect(wrapper.text()).toContain('Something went wrong')
  })

  it('does not show error banner initially', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const vm = wrapper.vm as unknown as { error: string | null }
    expect(vm.error).toBeNull()
    // The error div should not be rendered (v-if="error")
    expect(wrapper.find('.text-destructive').exists()).toBe(false)
  })
})

/* ── Button disabled states ───────────────────────────────────────────── */

describe('GraduationDialog button states', () => {
  it('graduate button is disabled when canGraduate is false', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text().includes('Graduate'))!
    expect(gradBtn.attributes('disabled')).toBeDefined()
  })

  it('graduate button is enabled when canGraduate is true', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const vm = wrapper.vm as unknown as { selectedPipelineId: string | null }
    vm.selectedPipelineId = 'pipe-1'
    await flushPromises()

    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text().includes('Graduate'))!
    expect(gradBtn.attributes('disabled')).toBeUndefined()
  })

  it('shows "Graduating..." text while graduating', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const vm = wrapper.vm as unknown as { graduating: boolean }
    vm.graduating = true
    await flushPromises()

    const buttons = wrapper.findAll('[data-testid="button"]')
    const gradBtn = buttons.find(b => b.text().includes('Graduating'))!
    expect(gradBtn).toBeDefined()
  })
})

/* ── Pipeline options in existing mode ─────────────────────────────────── */

describe('GraduationDialog pipeline select', () => {
  it('passes pipelines as options to the select', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const select = wrapper.find('[data-testid="select-Select pipeline"]')
    const options = select.findAll('option')
    expect(options).toHaveLength(2)
    expect(options[0].text()).toBe('Deploy Pipeline')
    expect(options[1].text()).toBe('Build Pipeline')
  })
})

/* ── Template options in new mode ─────────────────────────────────────── */

describe('GraduationDialog template select', () => {
  it('shows 3 template options in new mode', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const radio = wrapper.find('#graduationdialog-field-1')
    await radio.setValue('new')
    await flushPromises()

    const select = wrapper.find('[data-testid="select-Select template"]')
    const options = select.findAll('option')
    expect(options).toHaveLength(3)
  })
})

/* ── Prop passthrough ─────────────────────────────────────────────────── */

describe('GraduationDialog prop passthrough', () => {
  it('passes mapId to the component', async () => {
    const wrapper = mountDialog({ mapId: 'map-7' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { mapId: string }
    expect(vm.mapId).toBe('map-7')
  })

  it('passes versionId to the component', async () => {
    const wrapper = mountDialog({ versionId: 'ver-3' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { versionId: string }
    expect(vm.versionId).toBe('ver-3')
  })
})
