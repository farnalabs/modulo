import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import FilterBar from '../components/shared/FilterBar.vue'

const selectStub = defineComponent({
  inheritAttrs: false,
  props: {
    modelValue: { type: [String, Number], default: undefined },
    options: { type: Array, default: () => [] },
    placeholder: { type: String, default: '' },
  },
  emits: ['update:model-value'],
  computed: {
    displayValue(): string {
      const current = (this.options as any[])?.find((o: any) => o.value === this.modelValue)
      return current ? String((current as any).label) : String(this.placeholder ?? '')
    },
  },
  template: `
    <div class="p-select" :data-testid="$attrs['data-testid']">
      <span class="p-select-label">{{ displayValue }}</span>
      <div class="p-select-options">
        <div class="p-select-header"><slot name="header" /></div>
        <div v-for="opt in options" :key="opt.value" class="p-select-option" :data-value="opt.value" @click="$emit('update:model-value', opt.value)">
          <slot name="option" :option="opt">{{ opt.label }}</slot>
        </div>
      </div>
    </div>
  `,
})

function mountFilterBar() {
  return mount(FilterBar, {
    global: { stubs: { Select: selectStub } },
    props: {
      filters: [
        {
          key: 'status',
          label: 'Status',
          options: [
            { value: 'running', label: 'Running' },
            { value: 'complete', label: 'Complete' },
          ],
        },
      ],
      filterValues: { status: '' },
    },
  })
}

describe('FilterBar', () => {
  it('renders the default "All" selection when no filter is set', () => {
    const wrapper = mountFilterBar()
    expect(wrapper.find('.p-select-label').text()).toBe('All Status')
  })

  it('renders the "__all__" option with a distinct "All <label>" text', () => {
    const wrapper = mountFilterBar()
    const options = wrapper.findAll('.p-select-option')
    const allOption = options.find((o) => o.attributes('data-value') === '__all__')
    expect(allOption).toBeTruthy()
    expect(allOption!.text()).toContain('All Status')
  })

  it('does not duplicate the placeholder text on any option', () => {
    const wrapper = mountFilterBar()
    const options = wrapper.findAll('.p-select-option')
    for (const option of options) {
      expect(option.text()).not.toBe('Status')
    }
  })

  it('derives the noun from the filter key when the label is bare "All"', () => {
    const wrapper = mount(FilterBar, {
      global: { stubs: { Select: selectStub } },
      props: {
        filters: [
          {
            key: 'status',
            label: 'All',
            options: [{ value: 'running', label: 'Running' }],
          },
        ],
        filterValues: { status: '' },
      },
    })
    const allOption = wrapper.findAll('.p-select-option').find((o) => o.attributes('data-value') === '__all__')
    expect(allOption!.text()).toContain('All status')
  })

  it('keeps an already-prefixed "All ..." label verbatim', () => {
    const wrapper = mount(FilterBar, {
      global: { stubs: { Select: selectStub } },
      props: {
        filters: [
          {
            key: 'level',
            label: 'All levels',
            options: [{ value: 'error', label: 'Error' }],
          },
        ],
        filterValues: { level: '' },
      },
    })
    const allOption = wrapper.findAll('.p-select-option').find((o) => o.attributes('data-value') === '__all__')
    expect(allOption!.text()).toContain('All levels')
  })

  it('renders the filter name as a non-selectable label at the top of the dropdown', () => {
    const wrapper = mountFilterBar()
    const header = wrapper.find('.p-select-header')
    expect(header.exists()).toBe(true)
    expect(header.text()).toBe('Status')
  })

  it('emits update:filter with empty string for the "All" option', async () => {
    const wrapper = mountFilterBar()
    const allOption = wrapper.findAll('.p-select-option').find((o) => o.attributes('data-value') === '__all__')!
    await allOption.trigger('click')
    expect(wrapper.emitted('update:filter')).toBeTruthy()
    expect(wrapper.emitted('update:filter')![0]).toEqual(['status', ''])
  })
})
