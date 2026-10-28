import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, nextTick, ref } from 'vue'
import { writeFileSync } from 'node:fs'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Card from 'primevue/card'
import Badge from 'primevue/badge'
import Menu from 'primevue/menu'
import Select from 'primevue/select'
import Tabs from 'primevue/tabs'
import TabList from 'primevue/tablist'
import Tab from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel from 'primevue/tabpanel'
import Popover from 'primevue/popover'
import Tooltip from 'primevue/tooltip'

/**
 * PrimeVue component DOM signature (FAR-2205).
 *
 * A PrimeVue major-version change can alter a component's rendered structure
 * without a single app-level test failing: the wrapper API, the class list and
 * the `data-pc-*` pass-through attributes are the contract every one of our
 * views depends on. This spec mounts all fifteen components the app uses, in
 * the variants the app actually uses, and pins the rendered DOM.
 *
 * Two artefacts come out of the same mount:
 *
 *   1. `toMatchSnapshot()` — the permanent regression guard, checked into
 *      `src/__tests__/__snapshots__/`.
 *   2. When `PRIMEVUE_DOM_OUT` is set, a normalised JSON signature per
 *      component, written there. The v5 and v4 signatures are diffed by
 *      `scripts/compare-primevue-baseline.mjs` to produce the downgrade's DOM
 *      delta (the committed snapshot can only hold one version at a time, so
 *      JSON is the cross-version comparison record).
 *
 * Everything generated per-run (PrimeVue's `pv_id_*` element ids) is
 * normalised so a snapshot cannot churn on an id sequence.
 */

const OUT = process.env.PRIMEVUE_DOM_OUT
const signatures: Record<string, string> = {}

/** Collapse whitespace and normalise PrimeVue's generated ids. */
function normalise(html: string): string {
  return html
    .replace(/pv_id_?\d+/g, 'pv_id_N')
    .replace(/pc\d+/g, 'pcN')
    .replace(/pd\d+/g, 'pdN')
    .replace(/\s+/g, ' ')
    .replace(/>\s+</g, '><')
    .trim()
}

/** Mount, record, snapshot. */
function capture(name: string, html: string): void {
  const normalised = normalise(html)
  signatures[name] = normalised
  expect(normalised).toMatchSnapshot(name)
}

const OPTIONS = [
  { label: 'Alpha', value: 'alpha' },
  { label: 'Beta', value: 'beta' },
  { label: 'Gamma', value: 'gamma' },
]

const MENU_MODEL = [
  { label: 'Open', icon: 'pi pi-external-link' },
  { label: 'Duplicate', icon: 'pi pi-copy' },
  { separator: true },
  { label: 'Delete', icon: 'pi pi-trash', severity: 'danger' },
]

describe('PrimeVue component DOM signature', () => {
  it('button — the seven variants the app renders', () => {
    const variants = [
      { label: 'default', props: {} },
      { label: 'secondary-outlined', props: { severity: 'secondary', outlined: true } },
      { label: 'danger', props: { severity: 'danger' } },
      { label: 'small', props: { size: 'small' } },
      { label: 'text', props: { text: true } },
      { label: 'link', props: { link: true } },
      { label: 'icon-only', props: { icon: 'pi pi-plus', 'aria-label': 'Add' } },
    ] as const

    for (const variant of variants) {
      const wrapper = mount(Button, {
        props: { ...variant.props },
        slots: variant.label === 'icon-only' ? {} : { default: 'Save' },
      })
      capture(`button.${variant.label}`, wrapper.html())
    }
  })

  it('dialog — header, body and footer', async () => {
    const wrapper = mount(Dialog, {
      props: { visible: true, header: 'Confirm', modal: false, closable: true },
      slots: { default: '<p>Body copy</p>', footer: '<button>Cancel</button><button>OK</button>' },
      attachTo: document.body,
    })
    // The dialog is teleported to <body> (appendTo default), so the wrapper's
    // own html is a teleport placeholder. Capture the rendered subtree.
    await vi.waitFor(() => {
      expect(document.body.querySelector('.p-dialog')).not.toBeNull()
    })
    const dialog = document.body.querySelector('.p-dialog')
    capture('dialog.default', dialog ? dialog.outerHTML : 'NO_DIALOG')
    wrapper.unmount()
  })

  it('inputtext — default, valued and disabled', () => {
    const variants = [
      { label: 'default', props: {} },
      { label: 'valued', props: { modelValue: 'hello' } },
      { label: 'disabled', props: { disabled: true } },
    ] as const

    for (const variant of variants) {
      const wrapper = mount(InputText, { props: { ...variant.props } })
      capture(`inputtext.${variant.label}`, wrapper.html())
    }
  })

  it('card — title, subtitle, content and footer', () => {
    const wrapper = mount(Card, {
      slots: {
        title: 'Run summary',
        subtitle: 'Last 7 days',
        content: '<p>Content body</p>',
        footer: '<button>Open</button>',
      },
    })
    capture('card.default', wrapper.html())
  })

  it('badge — default, severity and pill variants', () => {
    const variants = [
      { label: 'default', props: {} },
      { label: 'secondary', props: { severity: 'secondary' } },
      { label: 'danger', props: { severity: 'danger' } },
      { label: 'success', props: { severity: 'success' } },
      { label: 'pill', props: { size: 'small' } },
    ] as const

    for (const variant of variants) {
      const wrapper = mount(Badge, {
        props: { ...variant.props },
        slots: { default: 'New' },
      })
      capture(`badge.${variant.label}`, wrapper.html())
    }
  })

  it('menu — inline popup model with a separator', () => {
    const wrapper = mount(Menu, {
      props: { model: MENU_MODEL, popup: false },
    })
    capture('menu.default', wrapper.html())
  })

  it('select — closed, with options', () => {
    const wrapper = mount(Select, {
      props: { modelValue: 'alpha', options: OPTIONS, optionLabel: 'label', optionValue: 'value' },
    })
    capture('select.default', wrapper.html())
  })

  it('tabs — tablist, tabs and panels', () => {
    const wrapper = mount(
      defineComponent({
        components: { Tabs, TabList, Tab, TabPanels, TabPanel },
        setup() {
          const value = ref('one')
          return { value }
        },
        render() {
          return h(
            Tabs,
            { value: this.value, 'onUpdate:value': (v: string) => (this.value = v) },
            {
              default: () => [
                h(TabList, null, {
                  default: () => [
                    h(Tab, { value: 'one' }, { default: () => 'Schema' }),
                    h(Tab, { value: 'two' }, { default: () => 'Parameters' }),
                  ],
                }),
                h(TabPanels, null, {
                  default: () => [
                    h(TabPanel, { value: 'one' }, { default: () => 'Schema body' }),
                    h(TabPanel, { value: 'two' }, { default: () => 'Parameters body' }),
                  ],
                }),
              ],
            },
          )
        },
      }),
    )
    capture('tabs.default', wrapper.html())
  })

  it('tooltip — directive overlay on hover', async () => {
    const wrapper = mount(
      defineComponent({
        directives: { tooltip: Tooltip },
        template: '<button v-tooltip.top="\'Tooltip copy\'">Target</button>',
      }),
      { attachTo: document.body },
    )

    const target = wrapper.get('button')
    await target.trigger('mouseenter')

    // The overlay is teleported to <body> and mounted on the next macrotask;
    // wait for it rather than sleeping.
    await vi.waitFor(() => {
      expect(document.body.querySelector('.p-tooltip')).not.toBeNull()
    })

    // Capture only the tooltip subtree so the surrounding test harness HTML
    // does not enter the signature. Tooltip's inline style is viewport
    // placement + the fade-in animation's current opacity, both of which are
    // environment-dependent and not part of the component contract; the
    // theming contract lives in the `--p-tooltip-*` tokens.
    const tooltip = document.body.querySelector('.p-tooltip')
    const tooltipHtml = tooltip ? tooltip.outerHTML.replace(/\sstyle="[^"]*"/, '') : 'NO_TOOLTIP'
    capture('tooltip.default', tooltipHtml)
    wrapper.unmount()
  })

  it('popover — overlay content', async () => {
    const wrapper = mount(
      defineComponent({
        components: { Popover },
        setup() {
          const popover = ref<InstanceType<typeof Popover> | null>(null)
          const toggle = (event: MouseEvent) => popover.value?.toggle(event)
          return { popover, toggle }
        },
        template: `
          <div>
            <button class="popover-trigger" @click="toggle">Open</button>
            <Popover ref="popover"><p>Popover body</p></Popover>
          </div>
        `,
      }),
      { attachTo: document.body },
    )

    await wrapper.get('button.popover-trigger').trigger('click')
    await nextTick()

    await vi.waitFor(() => {
      expect(document.body.querySelector('.p-popover')).not.toBeNull()
    })

    const popover = document.body.querySelector('.p-popover')
    capture('popover.default', popover ? popover.outerHTML : 'NO_POPOVER')
    wrapper.unmount()
  })

  it('writes the cross-version signature file when PRIMEVUE_DOM_OUT is set', () => {
    if (OUT) writeFileSync(OUT, JSON.stringify(signatures, null, 1))
    expect(Object.keys(signatures).length).toBeGreaterThan(15)
  })
})
