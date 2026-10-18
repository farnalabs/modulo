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
// host reproduces that DOM shape without needing PrimeVue's Dialog. The host is
// deliberately class-only: the unit environment does not load PrimeVue's
// stylesheet, so the container is recognised from that class contract (see the
// fallback in AppSelect.vue) - which is exactly the DOM a browser would give
// us with the real stylesheet applied.
const InsideDialogContent = defineComponent({
  components: { AppSelect },
  props: { appendTo: { type: String, default: undefined } },
  template: '<div class="p-dialog-content"><AppSelect :model-value="\'\'" :append-to="appendTo" /></div>',
})

// FAR-1920: a non-dialog clipping container — a custom panel with a clipped,
// bounded scroll area (`overflow: auto`) — must resolve to `'body'` too. The
// host deliberately carries no PrimeVue class, so this pins the predicate as
// general rather than class-specific.
const InsideCustomClippingPanel = defineComponent({
  components: { AppSelect },
  props: { appendTo: { type: String, default: undefined } },
  template:
    '<div class="custom-panel" style="overflow: auto"><AppSelect :model-value="\'\'" :append-to="appendTo" /></div>',
})

// FAR-1920: a browser exposes `overflow` as its `overflow-x`/`overflow-y`
// longhands, so the longhand arm of the predicate is the path a real
// dialog/drawer/custom box takes. jsdom does not expand the `overflow`
// shorthand (its longhands stay `visible`), so this host sets a longhand
// directly to exercise that arm.
const InsideLonghandClippingPanel = defineComponent({
  components: { AppSelect },
  props: { appendTo: { type: String, default: undefined } },
  template:
    '<div class="custom-panel" style="overflow-x: hidden"><AppSelect :model-value="\'\'" :append-to="appendTo" /></div>',
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

describe('AppSelect overlay append target — any clipping ancestor (FAR-1920)', () => {
  it('portals to body inside a non-dialog custom overflow panel', async () => {
    // The durable predicate is "does this Select sit inside a clipping
    // ancestor", not "is it in a dialog". A custom `overflow: auto` panel is
    // the same defect class one container over, and must be covered without a
    // new special case.
    const wrapper = mount(InsideCustomClippingPanel)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('body')
  })

  it('portals to body when the clipping ancestor is several levels up', async () => {
    // A Drawer / sidebar wraps its content, so the clipping box is not always
    // the Select's direct parent. Detection walks the whole chain.
    const Nested = defineComponent({
      components: { AppSelect },
      template:
        '<div style="overflow: hidden"><div class="drawer-body"><div><AppSelect :model-value="\'\'" /></div></div></div>',
    })
    const wrapper = mount(Nested)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('body')
  })

  it('portals to body for a p-*-content ancestor whose computed overflow is inconclusive', async () => {
    // The computed overflow is the primary signal, but this unit environment
    // loads no PrimeVue stylesheet, so a `.p-dialog-content` box computes
    // `overflow: visible` here and the class-shape fallback is the only signal
    // left. In a browser the same box reports `overflow-y: auto` from the
    // stylesheet and computed overflow decides — but the class fallback still
    // marks any remaining `p-*-content` box clipping, so the target is `body`.
    // `await nextTick()` is required: `onMounted` sets the decision a tick after
    // mount, and without it the assertion would read the pre-mount `'self'`.
    const Unclipped = defineComponent({
      components: { AppSelect },
      template:
        '<div class="p-dialog-content page-shell"><div class="drawer-surface"><AppSelect :model-value="\'\'" /></div></div>',
    })
    const wrapper = mount(Unclipped)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('body')
  })

  it('portals to body for a clipping ancestor even without a recognised class', async () => {
    const Clipped = defineComponent({
      components: { AppSelect },
      template: '<div class="page-shell" style="overflow: hidden"><AppSelect :model-value="\'\'" /></div>',
    })
    const wrapper = mount(Clipped)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('body')
  })

  it('portals to body when an ancestor clips a single axis via a longhand', async () => {
    // The browser-real path: `overflow: auto` is reported as the longhands
    // `overflow-x`/`overflow-y`, so a container that clips one axis only must
    // still be recognised. jsdom keeps the longhands `visible` for the
    // shorthand, so this host pins the longhand arm of the predicate directly.
    const wrapper = mount(InsideLonghandClippingPanel)
    await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(innerSelect).props('appendTo')).toBe('body')
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
