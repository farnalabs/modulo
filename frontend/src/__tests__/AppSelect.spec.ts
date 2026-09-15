import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import AppSelect from '../components/shared/AppSelect.vue'

// Stub the inner PrimeVue Select so the wrapper's own behaviour is what we
// exercise (label forwarding, slot passthrough, appendTo default).
const selectStub = defineComponent({
  inheritAttrs: false,
  props: {
    appendTo: { type: String, default: undefined },
    ariaLabel: { type: String, default: undefined },
  },
  emits: ['update:model-value'],
  template: `
    <div
      class="p-select"
      v-bind="$attrs"
      :append-to="appendTo"
      :aria-label="ariaLabel"
    >
      <slot name="header" />
      <slot name="option" :option="{ value: 'running', label: 'Running' }" />
    </div>
  `,
})

function mountAppSelect(options: {
  props?: Record<string, unknown>
  attrs?: Record<string, unknown>
  slots?: Record<string, string>
} = {}) {
  return mount(AppSelect, {
    global: { stubs: { Select: selectStub } },
    props: options.props,
    attrs: options.attrs,
    slots: options.slots,
  })
}

describe('AppSelect', () => {
  it('defaults appendTo to "self" so the overlay anchors inside the wrapper', () => {
    const wrapper = mountAppSelect()
    expect(wrapper.find('.p-select').attributes('append-to')).toBe('self')
  })

  it('forwards a custom appendTo value to the underlying Select', () => {
    const wrapper = mountAppSelect({ props: { appendTo: 'body' } })
    expect(wrapper.find('.p-select').attributes('append-to')).toBe('body')
  })

  it('uses an explicit label prop for the aria-label association', () => {
    const wrapper = mountAppSelect({ props: { label: 'Priority' } })
    expect(wrapper.find('.p-select').attributes('aria-label')).toBe('Priority')
  })

  it('falls back to an aria-label passed through $attrs', () => {
    const wrapper = mountAppSelect({ attrs: { 'aria-label': 'Level' } })
    expect(wrapper.find('.p-select').attributes('aria-label')).toBe('Level')
  })

  it('uses the generic default aria-label when none is provided', () => {
    const wrapper = mountAppSelect()
    expect(wrapper.find('.p-select').attributes('aria-label')).toBe('Select')
  })

  it('renders header and option slots passed through by the wrapper', () => {
    const wrapper = mountAppSelect({
      slots: {
        header: '<span class="hdr">Filters</span>',
        option: '<span class="opt">Running</span>',
      },
    })
    expect(wrapper.find('.hdr').text()).toBe('Filters')
    expect(wrapper.find('.opt').text()).toBe('Running')
  })
})
