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
 */
import { describe, it, expect } from 'vitest'
import { nextTick } from 'vue'
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

describe('AppSelect', () => {
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
