import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, nextTick } from 'vue'
import type { SchemaField } from '../../types/pipeline'
import FieldMappingPair from '../../components/pipeline/composite/FieldMappingPair.vue'

vi.mock('../../components/ui/button/Button.vue', () => ({
  default: {
    name: 'Button',
    props: ['variant', 'size', 'disabled'],
    template: '<button type="button" :disabled="disabled" @click="$emit(\'click\', $event)"><slot /></button>',
  },
}))

const mockSourceFields: SchemaField[] = [
  { name: 'title', type: 'string', description: null, required: true },
  { name: 'body', type: 'string', description: null, required: true },
  { name: 'score', type: 'number', description: null, required: false },
]

const mockTargetFields: SchemaField[] = [
  { name: 'title', type: 'string', description: null, required: true },
  { name: 'content', type: 'string', description: null, required: true },
  { name: 'rating', type: 'number', description: null, required: false },
]

describe('FieldMappingPair', () => {
  it('renders without crashing', () => {
    const wrapper = mount(FieldMappingPair, {
      props: {
        sourceFields: mockSourceFields,
        targetFields: mockTargetFields,
        mappings: {},
        direction: 'input',
      },
    })
    expect(wrapper.exists()).toBe(true)
  })

  it('shows passthrough badge when schemas are identical', () => {
    const identical: SchemaField[] = [
      { name: 'title', type: 'string', description: null, required: true },
      { name: 'body', type: 'string', description: null, required: false },
    ]
    const wrapper = mount(FieldMappingPair, {
      props: {
        sourceFields: identical,
        targetFields: [...identical],
        mappings: {},
        direction: 'input',
      },
    })
    expect(wrapper.text()).toContain('Passthrough')
  })

  it('does not show passthrough badge when schemas differ', () => {
    const wrapper = mount(FieldMappingPair, {
      props: {
        sourceFields: mockSourceFields,
        targetFields: mockTargetFields,
        mappings: {},
        direction: 'input',
      },
    })
    expect(wrapper.text()).not.toContain('Passthrough')
  })

  it('renders existing mappings', () => {
    const wrapper = mount(FieldMappingPair, {
      props: {
        sourceFields: mockSourceFields,
        targetFields: mockTargetFields,
        mappings: { title: 'title' },
        direction: 'input',
      },
    })
    expect(wrapper.text()).toContain('title')
    expect(wrapper.text()).toContain('1 field mapped')
  })

  it('shows Add mapping button when there are unmapped fields', () => {
    const wrapper = mount(FieldMappingPair, {
      props: {
        sourceFields: mockSourceFields,
        targetFields: mockTargetFields,
        mappings: {},
        direction: 'input',
      },
    })
    expect(wrapper.text()).toContain('Add mapping')
  })

  it('emits update:mappings when remove button is clicked', async () => {
    const wrapper = mount(FieldMappingPair, {
      props: {
        sourceFields: mockSourceFields,
        targetFields: mockTargetFields,
        mappings: { title: 'title' },
        direction: 'input',
      },
    })
    const buttons = wrapper.findAll('button')
    const removeBtn = buttons.find(b => b.text().includes('×') || b.html().includes('M18 6L6 18'))
    if (removeBtn) {
      await removeBtn.trigger('click')
      await nextTick()
      const emitted = wrapper.emitted('update:mappings')
      expect(emitted).toBeTruthy()
      if (emitted) {
        const mappings = emitted[0][0] as Record<string, string>
        expect(mappings.title).toBeUndefined()
      }
    }
  })

  it('auto-maps fields with matching name and type', async () => {
    const wrapper = mount(FieldMappingPair, {
      props: {
        sourceFields: mockSourceFields,
        targetFields: mockTargetFields,
        mappings: {},
        direction: 'input',
      },
    })
    const autoBtn = wrapper.findAll('button').find(b => b.text().includes('Auto-map'))
    expect(autoBtn).toBeTruthy()
    if (autoBtn) {
      await autoBtn.trigger('click')
      await nextTick()
      const emitted = wrapper.emitted('update:mappings')
      expect(emitted).toBeTruthy()
      if (emitted) {
        const mappings = emitted[0][0] as Record<string, string>
        expect(mappings.title).toBe('title')
      }
    }
  })

  it('clears mappings when Clear button is clicked', async () => {
    const wrapper = mount(FieldMappingPair, {
      props: {
        sourceFields: mockSourceFields,
        targetFields: mockTargetFields,
        mappings: { title: 'title', body: 'content' },
        direction: 'input',
      },
    })
    const clearBtn = wrapper.findAll('button').find(b => b.text().includes('Clear'))
    expect(clearBtn).toBeTruthy()
    if (clearBtn) {
      await clearBtn.trigger('click')
      await nextTick()
      const emitted = wrapper.emitted('update:mappings')
      expect(emitted).toBeTruthy()
      if (emitted) {
        const mappings = emitted[0][0] as Record<string, string>
        expect(Object.keys(mappings).length).toBe(0)
      }
    }
  })

  it('renders unique control ids across the input and output copies', async () => {
    // One app instance renders both copies, mirroring SchemaMappingPanel, so
    // PrimeVue's own generated ids are shared across the tree as they are in
    // production (two separate mount() calls would each restart that counter).
    const Harness = defineComponent({
      components: { FieldMappingPair },
      setup() {
        return { sourceFields: mockSourceFields, targetFields: mockTargetFields, mappings: {} }
      },
      template: `
        <FieldMappingPair :source-fields="sourceFields" :target-fields="targetFields" :mappings="mappings" direction="input" />
        <FieldMappingPair :source-fields="sourceFields" :target-fields="targetFields" :mappings="mappings" direction="output" />
      `,
    })
    const container = document.createElement('div')
    document.body.appendChild(container)
    const wrapper = mount(Harness, { attachTo: container })

    const addButtons = wrapper.findAll('button').filter(b => b.text().includes('Add mapping'))
    expect(addButtons).toHaveLength(2)
    for (const button of addButtons) {
      await button.trigger('click')
    }
    await nextTick()

    const ids = Array.from(container.querySelectorAll<HTMLElement>('[id]')).map(el => el.id)
    expect(ids).toContain('fieldmappingpair-input-source')
    expect(ids).toContain('fieldmappingpair-input-target')
    expect(ids).toContain('fieldmappingpair-output-source')
    expect(ids).toContain('fieldmappingpair-output-target')
    expect(new Set(ids).size).toBe(ids.length)

    wrapper.unmount()
    container.remove()
  })

  it('opens picker when Add mapping is clicked', async () => {
    const wrapper = mount(FieldMappingPair, {
      props: {
        sourceFields: mockSourceFields,
        targetFields: mockTargetFields,
        mappings: {},
        direction: 'input',
      },
    })
    const addBtn = wrapper.findAll('button').find(b => b.text().includes('Add mapping'))
    expect(addBtn).toBeTruthy()
    if (addBtn) {
      await addBtn.trigger('click')
      await nextTick()
      expect(wrapper.text()).toContain('Source field')
      expect(wrapper.text()).toContain('Target field')
    }
  })
})
