import { describe, it, expect } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent } from 'vue'
import FilterBar from '../components/shared/FilterBar.vue'
import AppSelect from '../components/shared/AppSelect.vue'

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

function mountFilterBar(options: { search?: { placeholder: string } } = {}) {
  return mount(FilterBar, {
    global: { stubs: { Select: selectStub } },
    props: {
      search: options.search,
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

  it('renders the filter name as a non-selectable label at the top of the dropdown', async () => {
    const wrapper = mountFilterBar()
    const header = wrapper.find('.p-select-header')
    expect(header.exists()).toBe(true)
    expect(header.text()).toBe('Status')

    // FAR-312: the label is a padded, styled div — never an option itself.
    const label = wrapper.find('[data-testid="filter-bar-label-status"]')
    expect(label.exists()).toBe(true)
    expect(label.text()).toBe('Status')
    expect(label.classes()).toEqual(expect.arrayContaining(['px-3.5', 'text-xs', 'text-muted-foreground']))
    expect(label.attributes('data-value')).toBeUndefined()
    expect(label.element.closest('.p-select-option')).toBeNull()
    await label.trigger('click')
    expect(wrapper.emitted('update:filter')).toBeFalsy()
  })

  it('hides the top label when the filter label already reads "All ..."', () => {
    const wrapper = mount(FilterBar, {
      global: { stubs: { Select: selectStub } },
      props: {
        filters: [
          { key: 'level', label: 'All levels', options: [{ value: 'error', label: 'Error' }] },
        ],
        filterValues: {},
      },
    })
    expect(wrapper.find('[data-testid="filter-bar-label-level"]').exists()).toBe(false)
  })

  // FAR-312 option spacing + reset divider are applied through PrimeVue's
  // passthrough (pt.option) rather than slot markup — assert the pt contract
  // directly, then exercise the real rendered overlay below.
  function filterSelectPt(wrapper: ReturnType<typeof mountFilterBar>) {
    const vm = wrapper.findComponent(AppSelect).vm as unknown as {
      $attrs: { pt?: { option?: (opts: unknown) => { style?: Record<string, string> } } }
    }
    return vm.$attrs.pt?.option
  }

  const ptParams = (index: number, optionValues: string[]) => ({
    context: { index, option: { value: optionValues[index] } },
    props: { options: optionValues.map((value) => ({ value })) },
  })

  it('gives filter select options extra vertical breathing room', () => {
    const wrapper = mountFilterBar()
    const optionPt = filterSelectPt(wrapper)
    expect(optionPt).toBeTypeOf('function')
    const attrs = optionPt!(ptParams(1, ['__all__', 'running']))
    expect(attrs?.style?.paddingTop).toBe('0.375rem')
    expect(attrs?.style?.paddingBottom).toBe('0.375rem')
    expect(attrs?.style?.borderBottom).toBeUndefined()
  })

  it('adds a bottom divider to the "__all__" reset option when real options follow it', () => {
    const wrapper = mountFilterBar()
    const optionPt = filterSelectPt(wrapper)
    const attrs = optionPt!(ptParams(0, ['__all__', 'running', 'complete']))
    expect(attrs?.style?.borderBottom).toContain('1px solid')
  })

  it('omits the reset divider when the filter has no real options', () => {
    const wrapper = mount(FilterBar, {
      global: { stubs: { Select: selectStub } },
      props: {
        filters: [{ key: 'status', label: 'Status', options: [] }],
        filterValues: {},
      },
    })
    const optionPt = filterSelectPt(wrapper)
    const attrs = optionPt!(ptParams(0, ['__all__']))
    expect(attrs?.style?.borderBottom).toBeUndefined()
  })

  it('defaults to no reset divider when PrimeVue passes no option list', () => {
    const wrapper = mountFilterBar()
    const optionPt = filterSelectPt(wrapper)
    // props.options is absent -> the `?? 0` fallback applies (no divider, no throw).
    const attrs = optionPt!({ context: { index: 0 }, props: {} })
    expect(attrs?.style?.borderBottom).toBeUndefined()
    expect(attrs?.style?.paddingTop).toBe('0.375rem')
  })

  it('emits update:filter with empty string for the "All" option', async () => {
    const wrapper = mountFilterBar()
    const allOption = wrapper.findAll('.p-select-option').find((o) => o.attributes('data-value') === '__all__')!
    await allOption.trigger('click')
    expect(wrapper.emitted('update:filter')).toBeTruthy()
    expect(wrapper.emitted('update:filter')![0]).toEqual(['status', ''])
  })

  it('emits update:filter with the selected value (not "All") as a string', async () => {
    const wrapper = mountFilterBar()
    const runningOption = wrapper.findAll('.p-select-option').find((o) => o.attributes('data-value') === 'running')!
    await runningOption.trigger('click')
    expect(wrapper.emitted('update:filter')).toBeTruthy()
    expect(wrapper.emitted('update:filter')![0]).toEqual(['status', 'running'])
  })
})

describe('FilterBar responsive layout (FAR-627)', () => {
  it('stacks the bar as a column on mobile and wraps as a row from sm up', () => {
    const wrapper = mountFilterBar({ search: { placeholder: 'Search by pipeline name' } })
    expect(wrapper.classes()).toEqual(
      expect.arrayContaining(['flex', 'flex-col', 'sm:flex-row', 'sm:flex-wrap', 'sm:items-center', 'gap-2']),
    )
  })

  it('gives the search wrapper the full row width on mobile and restores intrinsic width at sm', () => {
    const wrapper = mountFilterBar({ search: { placeholder: 'Search by pipeline name' } })
    const searchWrapper = wrapper.find('[data-testid="filter-bar-search-wrapper"]')
    expect(searchWrapper.exists()).toBe(true)
    expect(searchWrapper.classes()).toEqual(expect.arrayContaining(['relative', 'w-full', 'sm:w-auto']))
    const input = wrapper.find('[data-testid="filter-bar-search"]')
    expect(input.exists()).toBe(true)
    expect(input.classes()).toEqual(expect.arrayContaining(['w-full', 'sm:w-auto']))
  })

  it('renders each select full width on mobile with compact behaviour from sm up', () => {
    const wrapper = mountFilterBar({ search: { placeholder: 'Search by pipeline name' } })
    const select = wrapper.find('[data-testid="filter-bar-status"]')
    expect(select.exists()).toBe(true)
    expect(select.classes()).toEqual(
      expect.arrayContaining(['w-full', 'sm:w-auto', 'sm:min-w-[140px]']),
    )
  })
})

// Composed-system check (FAR-312): the stub-based tests above prove FilterBar's
// slot/pt wiring, but only PrimeVue's real overlay proves the label renders at
// the top, outside the listbox, with the pt option styles applied. Same
// click-to-open pattern as SettingsErrorForwardersView.spec.ts.
describe('FilterBar real PrimeVue dropdown (FAR-312)', () => {
  function mountReal() {
    return mount(FilterBar, {
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
        filterValues: {},
      },
    })
  }

  it('opens with the padded filter-name label outside the listbox', async () => {
    const wrapper = mountReal()
    await wrapper.find('.p-select').trigger('click')
    await flushPromises()

    const label = wrapper.find('[data-testid="filter-bar-label-status"]')
    expect(label.exists()).toBe(true)
    expect(label.text()).toBe('Status')
    expect(label.element.closest('.p-select-overlay')).not.toBeNull()
    // Non-selectable: the label never sits inside the options listbox.
    expect(label.element.closest('[role="listbox"]')).toBeNull()
  })

  it('renders option elements carrying the FAR-312 spacing and reset divider', async () => {
    const wrapper = mountReal()
    await wrapper.find('.p-select').trigger('click')
    await flushPromises()

    const optionEls = wrapper.findAll('li[role="option"]')
    expect(optionEls).toHaveLength(3)
    const styleOf = (el: Element) => el.getAttribute('style') ?? ''

    // every option gets the extra vertical breathing room
    for (const option of optionEls) {
      expect(styleOf(option.element)).toContain('padding-top: 0.375rem')
      expect(styleOf(option.element)).toContain('padding-bottom: 0.375rem')
    }

    // the "__all__" reset option (first) carries the divider; real options do not
    expect(optionEls[0].text()).toContain('All Status')
    expect(styleOf(optionEls[0].element)).toContain('border-bottom')
    expect(styleOf(optionEls[1].element)).not.toContain('border-bottom')
  })
})
