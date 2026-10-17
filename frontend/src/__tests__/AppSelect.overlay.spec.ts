import { describe, it, expect } from 'vitest'
import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'
import AppSelect from '../components/shared/AppSelect.vue'
import FilterBar from '../components/shared/FilterBar.vue'

// Look the inner component up by name so this spec never imports
// `primevue/select` directly (appselect-guard.spec.ts forbids it).
const innerSelect = { name: 'Select' }

// A dialog renders its body inside `.p-dialog-content`, the scroll container
// that clips an in-place overlay (FAR-1908). Mounting AppSelect inside this
// host reproduces that DOM shape without needing PrimeVue's Dialog.
const InsideDialogContent = defineComponent({
  components: { AppSelect },
  props: { appendTo: { type: String, default: undefined } },
  template: '<div class="p-dialog-content"><AppSelect :model-value="\'\'" :append-to="appendTo" /></div>',
})

const OutsideDialogContent = defineComponent({
  components: { AppSelect },
  props: { appendTo: { type: String, default: undefined } },
  template: '<div class="page-shell"><AppSelect :model-value="\'\'" :append-to="appendTo" /></div>',
})

describe('AppSelect', () => {
  it('renders a PrimeVue Select and defaults appendTo to "self"', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      slots: { default: 'body' },
    })
    const select = wrapper.findComponent(innerSelect)
    expect(select.exists()).toBe(true)
    expect(select.props('appendTo')).toBe('self')
  })

  it('forwards an explicit appendTo prop to the underlying Select', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '', appendTo: 'body' },
    })
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('body')
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
    expect(wrapper.findComponent(innerSelect).props('ariaLabel')).toBe('Priority')
  })

  it('falls back to an aria-label passed through $attrs when no label prop is given', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
      attrs: { 'aria-label': 'Level' },
    })
    expect(wrapper.findComponent(innerSelect).props('ariaLabel')).toBe('Level')
  })

  it('uses the generic default aria-label when neither a label prop nor $attrs aria-label is present', () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: '' },
    })
    expect(wrapper.findComponent(innerSelect).props('ariaLabel')).toBe('Select')
  })
})

describe('AppSelect overlay append target (FAR-1908)', () => {
  it('portals to body when rendered inside a dialog content box', async () => {
    // The dialog's `.p-dialog-content` is a scroll container; an overlay kept
    // in place is clipped by it and its lower options are covered by the
    // footer. The target must be derived from that container, not opted into.
    const wrapper = mount(InsideDialogContent)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('body')
  })

  it('keeps self outside a dialog content box (FAR-851)', () => {
    const wrapper = mount(OutsideDialogContent)
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('self')
  })

  it('lets an explicit append-to prop win over the dialog-derived default', () => {
    const wrapper = mount(InsideDialogContent, { props: { appendTo: 'self' } })
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('self')
  })

  it('lets an explicit append-to prop win outside a dialog', () => {
    const wrapper = mount(OutsideDialogContent, { props: { appendTo: 'body' } })
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('body')
  })

  it('treats an empty-string append-to as absent and applies the dialog default', async () => {
    // `append-to=""` cannot be a valid PrimeVue target; it must fall back to
    // the derived default rather than bypassing the dialog escape hatch.
    const wrapper = mount(InsideDialogContent, { props: { appendTo: '' } })
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('body')
  })

  it('falls back to self when mounted with no dialog ancestor (FAR-851)', () => {
    const wrapper = mount(AppSelect, { props: { modelValue: '' } })
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('self')
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
