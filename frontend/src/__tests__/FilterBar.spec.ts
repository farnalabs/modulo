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
    <div class="p-select" v-bind="$attrs">
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

describe('FilterBar responsive layout (FAR-627)', () => {
  function mountResponsive() {
    return mount(FilterBar, {
      global: { stubs: { Select: selectStub } },
      props: {
        search: { placeholder: 'Search by pipeline name' },
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

  it('stacks the bar as a column on mobile and wraps as a row from sm up', () => {
    const wrapper = mountResponsive()
    expect(wrapper.classes()).toEqual(
      expect.arrayContaining(['flex', 'flex-col', 'sm:flex-row', 'sm:flex-wrap', 'sm:items-center', 'gap-2']),
    )
  })

  it('gives the search wrapper the full row width on mobile and restores intrinsic width at sm', () => {
    const wrapper = mountResponsive()
    const searchWrapper = wrapper.find('.relative')
    expect(searchWrapper.exists()).toBe(true)
    expect(searchWrapper.classes()).toEqual(expect.arrayContaining(['relative', 'w-full', 'sm:w-auto']))
  })

  it('keeps the search input full width on mobile and auto width at sm', () => {
    const wrapper = mountResponsive()
    const input = wrapper.find('[data-testid="filter-bar-search"]')
    expect(input.exists()).toBe(true)
    expect(input.classes()).toEqual(expect.arrayContaining(['w-full', 'sm:w-auto']))
  })

  it('renders each select full width on mobile with compact behaviour from sm up', () => {
    const wrapper = mountResponsive()
    const select = wrapper.find('[data-testid="filter-bar-status"]')
    expect(select.exists()).toBe(true)
    expect(select.classes()).toEqual(
      expect.arrayContaining(['w-full', 'sm:w-auto', 'min-w-0', 'sm:min-w-[140px]']),
    )
  })
})
