import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import FilterBar from '../components/shared/FilterBar.vue'

const selectStubs = {
  Select: { template: '<div><slot /></div>' },
  SelectTrigger: { template: '<button type="button"><slot /></button>' },
  SelectContent: { template: '<div><slot /></div>' },
  SelectItem: {
    props: ['value'],
    template: '<div data-testid="select-option" :data-value="value"><slot /></div>',
  },
  SelectLabel: { template: '<div data-testid="select-label"><slot /></div>' },
  SelectSeparator: { template: '<div data-testid="select-separator" />' },
  SelectValue: {
    props: ['placeholder'],
    template: '<span class="select-value">{{ placeholder }}</span>',
  },
}

function mountFilterBar() {
  return mount(FilterBar, {
    global: { stubs: selectStubs },
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
  it('renders the muted placeholder label', () => {
    const wrapper = mountFilterBar()
    expect(wrapper.find('.select-value').text()).toBe('Status')
  })

  it('renders the "__all__" option with a distinct "All <label>" text', () => {
    const wrapper = mountFilterBar()
    const options = wrapper.findAll('[data-testid="select-option"]')
    const allOption = options.find((o) => o.attributes('data-value') === '__all__')
    expect(allOption).toBeTruthy()
    expect(allOption!.text()).toBe('All Status')
    expect(allOption!.text()).not.toBe('Status')
  })

  it('does not duplicate the placeholder text on any option', () => {
    const wrapper = mountFilterBar()
    const options = wrapper.findAll('[data-testid="select-option"]')
    for (const option of options) {
      expect(option.text()).not.toBe('Status')
    }
  })

  it('derives the noun from the filter key when the label is bare "All"', () => {
    const wrapper = mount(FilterBar, {
      global: { stubs: selectStubs },
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
    const allOption = wrapper.findAll('[data-testid="select-option"]').find((o) => o.attributes('data-value') === '__all__')
    expect(allOption!.text()).toBe('All status')
  })

  it('keeps an already-prefixed "All ..." label verbatim', () => {
    const wrapper = mount(FilterBar, {
      global: { stubs: selectStubs },
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
    const allOption = wrapper.findAll('[data-testid="select-option"]').find((o) => o.attributes('data-value') === '__all__')
    expect(allOption!.text()).toBe('All levels')
  })

  it('renders the filter name as a non-selectable label at the top of the dropdown', () => {
    const wrapper = mountFilterBar()
    const label = wrapper.find('[data-testid="select-label"]')
    expect(label.exists()).toBe(true)
    expect(label.text()).toBe('Status')
  })

  it('renders a separator between the "All" option and the real options', () => {
    const wrapper = mountFilterBar()
    expect(wrapper.find('[data-testid="select-separator"]').exists()).toBe(true)
  })
})
