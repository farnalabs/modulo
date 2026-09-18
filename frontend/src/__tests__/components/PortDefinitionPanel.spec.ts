import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import type { ParameterPort } from '../../types/pipeline'

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }))

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn(),
    post: postMock,
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  }),
}))

// PrimeVue Select stub that behaves like the real component for object options
// carrying a `value` field: emits the raw option value on change.
const SelectStub = {
  name: 'Select',
  props: ['modelValue', 'options', 'optionLabel', 'optionValue', 'placeholder', 'ariaLabel'],
  emits: ['update:modelValue'],
  template: `
    <select data-testid="mock-select" :aria-label="ariaLabel || 'select'" @change="$emit('update:modelValue', $event.target.value)">
      <option v-for="o in options" :key="o.value ?? o" :value="o.value ?? o">{{ o.label ?? o }}</option>
    </select>`,
}

import PortDefinitionPanel from '../../components/pipeline/composite/PortDefinitionPanel.vue'

function makePort(overrides: Partial<ParameterPort> = {}): ParameterPort {
  return {
    id: 'port-1',
    name: 'model_temperature',
    label: 'Temperature',
    description: 'Controls randomness',
    type: 'number',
    required: false,
    default_value: 0.7,
    options: null,
    multiline: false,
    target_injection: { mode: 'prompt_replace', node_id: '', injection_point: 'prompt_template' },
    ...overrides,
  }
}

function mountPanel(ports: ParameterPort[] = []) {
  return mount(PortDefinitionPanel, {
    props: {
      ports,
      nodeIds: ['node-1', 'node-2'],
      nodes: [{ id: 'node-1' }, { id: 'node-2' }],
    },
    global: {
      stubs: { Select: SelectStub },
    },
  })
}

const nameInput = '#portdefinitionpanel-field-6'
const labelInput = '#portdefinitionpanel-field-5'
const descInput = '#portdefinitionpanel-field-4'
const defaultInput = '#portdefinitionpanel-field-1'

describe('PortDefinitionPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    postMock.mockResolvedValue({ ports: [] })
  })

  it('shows the empty state when there are no ports', () => {
    const wrapper = mountPanel([])
    expect(wrapper.text()).toContain('No parameter ports defined yet')
  })

  it('renders ports with label, required badge, type badge and template ref', () => {
    const wrapper = mountPanel([makePort({ required: true }), makePort({ id: 'port-2', name: 'tone', label: 'Tone', type: 'string', required: false })])
    expect(wrapper.text()).toContain('Temperature')
    expect(wrapper.text()).toContain('required')
    expect(wrapper.text()).toContain('number')
    expect(wrapper.text()).toContain('{{parameter.model_temperature}}')
    expect(wrapper.text()).toContain('{{parameter.tone}}')
    expect(wrapper.text()).toContain('Controls randomness')
  })

  it('shows a validation error when saving without name or label', async () => {
    const wrapper = mountPanel([])
    const addBtn = wrapper.findAll('button').find(b => b.text().includes('Add Port'))
    await addBtn!.trigger('click')
    const saveBtn = wrapper.findAll('button').find(b => b.text() === 'Add')
    await saveBtn!.trigger('click')
    expect(wrapper.text()).toContain('Name and label are required')
    expect(wrapper.emitted('update:ports')).toBeUndefined()
  })

  it('adds a string port and emits the updated list', async () => {
    const wrapper = mountPanel([])
    const addBtn = wrapper.findAll('button').find(b => b.text().includes('Add Port'))
    await addBtn!.trigger('click')
    await wrapper.find(nameInput).setValue('review_style')
    await wrapper.find(labelInput).setValue('Review Style')
    await wrapper.find(descInput).setValue('How thorough')
    const saveBtn = wrapper.findAll('button').find(b => b.text() === 'Add')
    await saveBtn!.trigger('click')
    const events = wrapper.emitted('update:ports')
    expect(events).toHaveLength(1)
    const ports = events![0][0] as ParameterPort[]
    expect(ports).toHaveLength(1)
    expect(ports[0].name).toBe('review_style')
    expect(ports[0].label).toBe('Review Style')
    expect(ports[0].description).toBe('How thorough')
    expect(ports[0].type).toBe('string')
    expect(ports[0].required).toBe(false)
    expect(ports[0].id).toEqual(expect.any(String))
    expect(ports[0].target_injection).toEqual({ mode: 'prompt_replace', node_id: '', injection_point: 'prompt_template' })
    expect(wrapper.find('.rounded-lg.border.bg-card').exists()).toBe(false)
  })

  it('converts numeric defaults for number ports', async () => {
    const wrapper = mountPanel([])
    const addBtn = wrapper.findAll('button').find(b => b.text().includes('Add Port'))
    await addBtn!.trigger('click')
    await wrapper.find(nameInput).setValue('temp')
    await wrapper.find(labelInput).setValue('Temp')
    await wrapper.find('[data-testid="mock-select"]').setValue('number')
    await wrapper.find(defaultInput).setValue('0.7')
    const saveBtn = wrapper.findAll('button').find(b => b.text() === 'Add')
    await saveBtn!.trigger('click')
    const ports = wrapper.emitted('update:ports')![0][0] as ParameterPort[]
    expect(ports[0].default_value).toBe(0.7)
  })

  it('converts boolean defaults for boolean ports and hides the default field', async () => {
    const wrapper = mountPanel([])
    const addBtn = wrapper.findAll('button').find(b => b.text().includes('Add Port'))
    await addBtn!.trigger('click')
    await wrapper.find(nameInput).setValue('verbose')
    await wrapper.find(labelInput).setValue('Verbose')
    await wrapper.find('[data-testid="mock-select"]').setValue('boolean')
    await nextTick()
    expect(wrapper.find(defaultInput).exists()).toBe(false)
    const saveBtn = wrapper.findAll('button').find(b => b.text() === 'Add')
    await saveBtn!.trigger('click')
    const ports = wrapper.emitted('update:ports')![0][0] as ParameterPort[]
    expect(ports[0].type).toBe('boolean')
    expect(ports[0].default_value).toBeUndefined()
  })

  it('marks a port required when the checkbox is ticked', async () => {
    const wrapper = mountPanel([])
    const addBtn = wrapper.findAll('button').find(b => b.text().includes('Add Port'))
    await addBtn!.trigger('click')
    await wrapper.find(nameInput).setValue('must_exist')
    await wrapper.find(labelInput).setValue('Must Exist')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    const saveBtn = wrapper.findAll('button').find(b => b.text() === 'Add')
    await saveBtn!.trigger('click')
    const ports = wrapper.emitted('update:ports')![0][0] as ParameterPort[]
    expect(ports[0].required).toBe(true)
  })

  it('edits an existing port keeping its id', async () => {
    const wrapper = mountPanel([makePort()])
    await wrapper.find('button[title="Edit"]').trigger('click')
    expect((wrapper.find(nameInput).element as HTMLInputElement).value).toBe('model_temperature')
    expect((wrapper.find(defaultInput).element as HTMLInputElement).value).toBe('0.7')
    await wrapper.find(labelInput).setValue('Temperature (edited)')
    const saveBtn = wrapper.findAll('button').find(b => b.text() === 'Update')
    await saveBtn!.trigger('click')
    const events = wrapper.emitted('update:ports')
    const ports = events![0][0] as ParameterPort[]
    expect(ports).toHaveLength(1)
    expect(ports[0].id).toBe('port-1')
    expect(ports[0].label).toBe('Temperature (edited)')
  })

  it('deletes a port', async () => {
    const wrapper = mountPanel([makePort(), makePort({ id: 'port-2', name: 'tone', label: 'Tone' })])
    await wrapper.find('button[title="Delete"]').trigger('click')
    const ports = wrapper.emitted('update:ports')![0][0] as ParameterPort[]
    expect(ports.map(p => p.id)).toEqual(['port-2'])
  })

  it('moves a port up and down, ignoring boundary moves', async () => {
    const wrapper = mountPanel([makePort(), makePort({ id: 'port-2', name: 'tone', label: 'Tone' })])
    // The second row's "Move up" is the non-boundary move that reorders.
    const upButtons = wrapper.findAll('button').filter(b => b.attributes('title') === 'Move up')
    await upButtons[1].trigger('click')
    const ports = wrapper.emitted('update:ports')![0][0] as ParameterPort[]
    expect(ports.map(p => p.id)).toEqual(['port-2', 'port-1'])

    const wrapper2 = mountPanel([makePort()])
    const upBtn2 = wrapper2.findAll('button').find(b => b.attributes('title') === 'Move up')
    await upBtn2!.trigger('click')
    expect(wrapper2.emitted('update:ports')).toBeUndefined()

    const wrapper3 = mountPanel([makePort()])
    const downBtn3 = wrapper3.findAll('button').find(b => b.attributes('title') === 'Move down')
    await downBtn3!.trigger('click')
    expect(wrapper3.emitted('update:ports')).toBeUndefined()
  })

  it('detects placeholders from the API and merges only new port names', async () => {
    postMock.mockResolvedValue({
      ports: [
        makePort({ id: 'd1', name: 'model_temperature', label: 'Temperature (dup)' }),
        makePort({ id: 'd2', name: 'tone', label: 'Tone', type: 'string' }),
      ],
    })
    const wrapper = mountPanel([makePort()])
    const detectBtn = wrapper.findAll('button').find(b => b.text() === 'Detect')
    await detectBtn!.trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/composite-templates/detect-params', {
      node_ids: ['node-1', 'node-2'],
      nodes: [{ id: 'node-1' }, { id: 'node-2' }],
    })
    const events = wrapper.emitted('update:ports')
    expect(events).toHaveLength(1)
    const ports = events![0][0] as ParameterPort[]
    expect(ports.map(p => p.name)).toEqual(['model_temperature', 'tone'])
  })

  it('emits nothing when detection returns no new ports', async () => {
    postMock.mockResolvedValue({ ports: [] })
    const wrapper = mountPanel([makePort()])
    const detectBtn = wrapper.findAll('button').find(b => b.text() === 'Detect')
    await detectBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.emitted('update:ports')).toBeUndefined()
  })

  it('retains existing ports when detection fails', async () => {
    postMock.mockRejectedValue(new Error('detection exploded'))
    const wrapper = mountPanel([makePort()])
    const detectBtn = wrapper.findAll('button').find(b => b.text() === 'Detect')
    await detectBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.emitted('update:ports')).toBeUndefined()
    expect(wrapper.text()).toContain('Temperature')
  })

  it('shows the detection loading state while the request is in flight', async () => {
    let resolvePost: (v: { ports: ParameterPort[] }) => void = () => undefined
    postMock.mockReturnValue(new Promise<{ ports: ParameterPort[] }>((resolve) => { resolvePost = resolve }))
    const wrapper = mountPanel([])
    const detectBtn = wrapper.findAll('button').find(b => b.text() === 'Detect')
    await detectBtn!.trigger('click')
    expect(wrapper.findAll('button').find(b => b.text() === '...')).toBeDefined()
    resolvePost({ ports: [] })
    await flushPromises()
    expect(wrapper.findAll('button').find(b => b.text() === 'Detect')).toBeDefined()
  })

  it('cancels the add form without emitting', async () => {
    const wrapper = mountPanel([])
    const addBtn = wrapper.findAll('button').find(b => b.text().includes('Add Port'))
    await addBtn!.trigger('click')
    await wrapper.find(nameInput).setValue('never_saved')
    const cancelBtn = wrapper.findAll('button').find(b => b.text() === 'Cancel')
    await cancelBtn!.trigger('click')
    expect(wrapper.find('.rounded-lg.border.bg-card').exists()).toBe(false)
    expect(wrapper.emitted('update:ports')).toBeUndefined()
  })
})
