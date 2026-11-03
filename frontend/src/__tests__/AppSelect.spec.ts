/**
 * AppSelect is the single wrapper around PrimeVue's Select (see
 * appselect-guard.spec.ts). Three behaviours matter and are easy to regress:
 *
 * 1. Event forwarding — the wrapper declares the Select events in
 *    `defineEmits`, which removes their listeners from `$attrs`, so it MUST
 *    re-emit each one from the inner Select or `v-model`/handlers silently
 *    stop working on every consumer (FAR-869).
 * 2. An explicit `aria-label` on the inner Select — a bare `v-bind="$attrs"`
 *    is invisible to SonarCloud's Web:InputWithoutLabelCheck, so the binding
 *    is written out and resolved from the consumer's `$attrs` (or a default).
 *    The inner Select is also wrapped in a `<label>` for the same reason.
 * 3. Label association — a non-editable PrimeVue Select renders a
 *    `<span role="combobox">`, which is NOT labelable, so `label[for]` alone
 *    never associates or focuses it. The spec asserts the RENDERED DOM: the
 *    combobox carries the consumer's id, its `aria-labelledby` resolves to the
 *    visible label's text, the wrapper carries no duplicate id, and clicking
 *    the label focuses the combobox. All four fail on the pre-fix component.
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

  it('maps a consumer id to the inner Select inputId so label[for] resolves', () => {
    // PrimeVue puts a fallthrough `id` on its root <div> and gives the
    // focusable combobox `id = labelId || inputId`. A label beside the Select
    // would therefore point at a div unless the id is re-applied as inputId.
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      attrs: { id: 'queue-filter' },
    })
    expect(innerSelect(wrapper).props('inputId')).toBe('queue-filter')
  })

  it('prefers an explicit input-id over a consumer id', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      attrs: { id: 'legacy-id', inputId: 'combobox-id' },
    })
    expect(innerSelect(wrapper).props('inputId')).toBe('combobox-id')
  })

  it('passes the resolved control id as labelId so PrimeVue can find label[for]', () => {
    // PrimeVue's own click bridge queries `label[for=<labelId>]`; leaving
    // labelId unset makes that query `label[for="undefined"]` and the bridge
    // binds nothing, which is the inert association this pins.
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      attrs: { id: 'queue-filter' },
    })
    expect(innerSelect(wrapper).props('labelId')).toBe('queue-filter')
  })

  it('merges a function-form consumer pt listContainer and adds tabindex', () => {
    const listContainer = (opts: { density?: string }) => ({ class: `density-${opts.density}` })
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      attrs: { pt: { listContainer } },
    })
    const pt = innerSelect(wrapper).props('pt') as {
      listContainer: (opts: { density?: string }) => Record<string, unknown>
    }
    expect(typeof pt.listContainer).toBe('function')
    expect(pt.listContainer({ density: 'compact' })).toEqual({
      class: 'density-compact',
      tabindex: '0',
    })
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

  it('forwards a consumer id as input-id and not as a root id', () => {
    const wrapper = mountAppSelect({ attrs: { id: 'org-select' } })
    const select = wrapper.find('.p-select')
    expect(select.attributes('input-id')).toBe('org-select')
    expect(select.attributes('id')).toBeUndefined()
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

/**
 * The label association, asserted against the RENDERED DOM rather than the
 * props handed to PrimeVue. `props('inputId')` being set proves only that a
 * prop was passed: it is satisfied by the pre-fix component, whose combobox
 * is a `<span role="combobox">` that a `label[for]` can neither associate with
 * nor focus (a span is not labelable).
 */
const LabelHost = defineComponent({
  components: { AppSelect },
  props: { controlId: { type: String, required: true } },
  template: `
    <div>
      <label :for="controlId">Level</label>
      <AppSelect :id="controlId" :options="[]" aria-label="Level" />
    </div>
  `,
})

function mountWithLabel(controlId: string) {
  return mount(LabelHost, { props: { controlId }, attachTo: document.body })
}

describe('AppSelect label association (rendered DOM)', () => {
  it('puts the consumer id on the combobox, not on the wrapper label', async () => {
    const wrapper = mountWithLabel('appselect-id-test')
    await nextTick()

    const combobox = wrapper.find('[role="combobox"]')
    expect(combobox.exists()).toBe(true)
    expect(combobox.attributes('id')).toBe('appselect-id-test')

    // Exactly one element in the document carries the id: with attribute
    // inheritance left on, the wrapper <label> would share it and
    // `label[for]` would resolve to the wrapper instead of the control.
    expect(document.querySelectorAll('#appselect-id-test')).toHaveLength(1)
    expect(document.querySelector('#appselect-id-test')?.tagName).toBe('SPAN')

    wrapper.unmount()
  })

  it('exposes the visible label as the combobox accessible name', async () => {
    const wrapper = mountWithLabel('appselect-name-test')
    await nextTick()

    const combobox = wrapper.find('[role="combobox"]')
    const labelledBy = combobox.attributes('aria-labelledby')
    expect(labelledBy).toBeTruthy()

    const labelEl = document.getElementById(labelledBy as string)
    expect(labelEl?.tagName).toBe('LABEL')
    expect(labelEl?.textContent?.trim()).toBe('Level')
    // The label is the consumer's own element, not a copy.
    expect(labelEl).toBe(wrapper.find('label[for="appselect-name-test"]').element)

    wrapper.unmount()
  })

  it('focuses the combobox when the visible label is clicked', async () => {
    const wrapper = mountWithLabel('appselect-click-test')
    await nextTick()

    const combobox = wrapper.find('[role="combobox"]')
    // A <span role="combobox"> is not labelable, so jsdom's native label
    // activation reaches no control - the click bridge is what makes the
    // label work.
    const labelEl = wrapper.find('label[for="appselect-click-test"]')
      .element as HTMLLabelElement
    expect(labelEl.control).toBeNull()
    expect(document.activeElement).not.toBe(combobox.element)

    await labelEl.click()
    await nextTick()

    expect(document.activeElement).toBe(combobox.element)

    wrapper.unmount()
  })

  it('keeps a consumer aria-labelledby and never overwrites an existing label id', async () => {
    const wrapper = mount(
      defineComponent({
        components: { AppSelect },
        template: `
          <div>
            <label id="explicit-label-id" for="appselect-explicit-test">Owner</label>
            <AppSelect id="appselect-explicit-test" aria-labelledby="explicit-label-id" />
          </div>
        `,
      }),
      { attachTo: document.body },
    )
    await nextTick()

    const combobox = wrapper.find('[role="combobox"]')
    expect(combobox.attributes('aria-labelledby')).toBe('explicit-label-id')
    expect(wrapper.find('label[for="appselect-explicit-test"]').attributes('id')).toBe(
      'explicit-label-id',
    )

    wrapper.unmount()
  })
})

/**
 * A host with two visible labels so the association can be MOVED when the
 * control id changes after mount. `syncLabelAssociation` must detach the old
 * label's click bridge before attaching the new one, or the stale label would
 * keep focusing the control it no longer describes.
 */
const TwoLabelHost = defineComponent({
  components: { AppSelect },
  props: { controlId: { type: String, required: true } },
  template: `
    <div>
      <label for="appselect-first-id">First</label>
      <label for="appselect-second-id">Second</label>
      <AppSelect :id="controlId" :options="[]" aria-label="Level" />
    </div>
  `,
})

describe('AppSelect label association when the control id changes', () => {
  it('re-resolves the label and moves the click bridge after an id change', async () => {
    const wrapper = mount(TwoLabelHost, {
      props: { controlId: 'appselect-first-id' },
      attachTo: document.body,
    })
    await nextTick()

    const firstLabel = wrapper.find('label[for="appselect-first-id"]')
      .element as HTMLLabelElement
    const secondLabel = wrapper.find('label[for="appselect-second-id"]')
      .element as HTMLLabelElement
    expect(wrapper.find('[role="combobox"]').attributes('aria-labelledby')).toBe(firstLabel.id)

    await wrapper.setProps({ controlId: 'appselect-second-id' })
    await nextTick()
    await nextTick()

    const combobox = wrapper.find('[role="combobox"]').element as HTMLElement
    expect(combobox.getAttribute('aria-labelledby')).toBe(secondLabel.id)

    // The old label's listener is gone; the new one focuses the combobox.
    firstLabel.click()
    expect(document.activeElement).not.toBe(combobox)
    secondLabel.click()
    expect(document.activeElement).toBe(combobox)

    wrapper.unmount()
  })

  it('falls back to manual id escaping when CSS.escape is unavailable', async () => {
    const originalEscape = CSS.escape
    const wrapper = mount(LabelHost, {
      props: { controlId: 'appselect-escape-first' },
      attachTo: document.body,
    })
    await nextTick()

    Object.defineProperty(CSS, 'escape', { configurable: true, writable: true, value: undefined })
    try {
      await wrapper.setProps({ controlId: 'appselect-escape-second' })
      await nextTick()
      await nextTick()

      const label = wrapper.find('label[for="appselect-escape-second"]')
        .element as HTMLLabelElement
      expect(label.id).toBeTruthy()
      expect(wrapper.find('[role="combobox"]').attributes('aria-labelledby')).toBe(label.id)
    } finally {
      Object.defineProperty(CSS, 'escape', {
        configurable: true,
        writable: true,
        value: originalEscape,
      })
      wrapper.unmount()
    }
  })
})
