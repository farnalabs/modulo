/**
 * AppSelect is the single wrapper around PrimeVue's Select (see
 * appselect-guard.spec.ts). Two behaviours matter and are easy to regress:
 *
 * 1. Event forwarding — the wrapper declares the Select events in
 *    `defineEmits`, which removes their listeners from `$attrs`, so it MUST
 *    re-emit each one from the inner Select or `v-model`/handlers silently
 *    stop working on every consumer (FAR-869).
 * 2. An explicit `aria-label` on the inner Select — a bare `v-bind="$attrs"`
 *    is invisible to SonarCloud's Web:InputWithoutLabelCheck, so the binding
 *    is written out and resolved from the consumer's `$attrs` (or a default).
 *    The inner Select is also wrapped in a `<label>` for the same reason.
 */
import { describe, it, expect } from 'vitest'
import { defineComponent, nextTick } from 'vue'
import { mount } from '@vue/test-utils'

import AppSelect from '../components/shared/AppSelect.vue'

// Look the inner component up by name so this spec never imports
// `primevue/select` directly (appselect-guard.spec.ts forbids it).
function innerSelect(wrapper: ReturnType<typeof mount>) {
  return wrapper.findComponent({ name: 'Select' })
}

const FORWARDED_EVENTS = [
  'update:modelValue',
  'blur',
  'focus',
  'change',
  'before-show',
  'before-hide',
  'show',
  'hide',
  'filter',
] as const

describe('AppSelect (real Select)', () => {
  it('renders the inner PrimeVue Select with appendTo defaulting to self', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      slots: { dropdownicon: '<span data-testid="ddi">v</span>' },
    })
    const select = innerSelect(wrapper)
    expect(select.exists()).toBe(true)
    expect(select.props('appendTo')).toBe('self')
    // named slots are forwarded generically to the inner Select
    expect(wrapper.find('[data-testid="ddi"]').exists()).toBe(true)
  })

  it('re-emits every declared event from the inner Select', async () => {
    const wrapper = mount(AppSelect, { props: { modelValue: '' } })
    const emit = (event: string, payload?: unknown) =>
      (
        innerSelect(wrapper).vm as unknown as {
          $emit: (e: string, v?: unknown) => void
        }
      ).$emit(event, payload)

    emit('update:modelValue', 'view-1')
    emit('blur', new Event('blur'))
    emit('focus', new Event('focus'))
    emit('change', 'changed')
    emit('before-show')
    emit('before-hide')
    emit('show')
    emit('hide')
    emit('filter', { value: 'q' })
    await nextTick()

    for (const event of FORWARDED_EVENTS) {
      expect(wrapper.emitted(event), `expected ${event} to be re-emitted`).toBeTruthy()
    }
    expect(wrapper.emitted('update:modelValue')).toEqual([['view-1']])
  })

  it('uses the aria-label passed through $attrs as the inner Select label', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      attrs: { 'aria-label': 'Level' },
    })
    expect(innerSelect(wrapper).props('ariaLabel')).toBe('Level')
  })

  it('falls back to a generic aria-label when the consumer passes none', () => {
    const wrapper = mount(AppSelect, { props: { modelValue: '' } })
    expect(innerSelect(wrapper).props('ariaLabel')).toBe('Select')
  })
})

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

describe('AppSelect (stubbed Select)', () => {
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
