import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import AppSelect from '../components/shared/AppSelect.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import Select from 'primevue/select'

describe('AppSelect', () => {
  it('renders a PrimeVue Select and defaults appendTo to "self"', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      slots: { default: 'body' },
    })
    const select = wrapper.findComponent(Select)
    expect(select.exists()).toBe(true)
    expect(select.props('appendTo')).toBe('self')
  })

  it('forwards an explicit appendTo prop to the underlying Select', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '', appendTo: 'body' },
    })
    expect(wrapper.findComponent(Select).props('appendTo')).toBe('body')
  })

  it('forwards scoped slots (e.g. dropdownicon) to the underlying Select', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '', appendTo: 'self' },
      slots: { dropdownicon: '<span data-testid="ddi">v</span>' },
    })
    expect(wrapper.find('[data-testid="ddi"]').exists()).toBe(true)
  })

  it('uses an explicit label prop as the resolved aria-label on the real Select', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '', label: 'Priority' },
    })
    expect(wrapper.findComponent(Select).props('ariaLabel')).toBe('Priority')
  })

  it('falls back to an aria-label passed through $attrs when no label prop is given', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      attrs: { 'aria-label': 'Level' },
    })
    expect(wrapper.findComponent(Select).props('ariaLabel')).toBe('Level')
  })

  it('uses the generic default aria-label when neither a label prop nor $attrs aria-label is present', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
    })
    expect(wrapper.findComponent(Select).props('ariaLabel')).toBe('Select')
  })
})

describe('FilterBar select overlay anchoring (FAR-851)', () => {
  const filters = [
    { key: 'status', label: 'Status', options: [{ value: 'open', label: 'Open' }] },
  ]

  it('renders AppSelect so the dropdown overlay anchors to its trigger', () => {
    const wrapper = mount(FilterBar, {
      props: { filters, filterValues: {} },
    })
    expect(wrapper.findComponent(AppSelect).exists()).toBe(true)
  })

  it('emits update:filter with the raw value when a real option is chosen', async () => {
    const wrapper = mount(FilterBar, {
      props: { filters, filterValues: {} },
    })
    await wrapper.findComponent(AppSelect).vm.$emit('update:model-value', 'open')
    expect(wrapper.emitted('update:filter')).toBeTruthy()
    expect(wrapper.emitted('update:filter')?.[0]).toEqual(['status', 'open'])
  })

  it('emits update:filter with an empty string when the ALL sentinel is chosen', async () => {
    const wrapper = mount(FilterBar, {
      props: { filters, filterValues: {} },
    })
    await wrapper.findComponent(AppSelect).vm.$emit('update:model-value', '__all__')
    expect(wrapper.emitted('update:filter')?.[0]).toEqual(['status', ''])
  })
})
