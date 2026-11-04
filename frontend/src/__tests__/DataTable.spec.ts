import { describe, it, expect } from 'vitest'
import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import DataTable, { type Column } from '../components/ui/data-table/DataTable.vue'

const columns: Column[] = [
  { key: 'name', label: 'Name', sortable: true },
  { key: 'value', label: 'Value', sortable: false },
]

const rows = [
  { name: 'beta', value: 2 },
  { name: 'alpha', value: 1 },
]

function mountTable() {
  return mount(DataTable, {
    props: { columns, rows, rowClickable: true },
  })
}

function headerCells(wrapper: ReturnType<typeof mountTable>) {
  return wrapper.findAll('thead th')
}

describe('DataTable keyboard accessibility', () => {
  it('exposes a discoverable horizontal scroll region', () => {
    const wrapper = mountTable()
    const region = wrapper.find('[aria-describedby^="data-table-scroll-instructions-"]')
    expect(region.attributes('tabindex')).toBe('0')
    expect(region.attributes('aria-describedby')).toMatch(/^data-table-scroll-instructions-/)
    expect(wrapper.find(`#${region.attributes('aria-describedby')}`).text()).toContain('Swipe horizontally')
    expect(wrapper.find('table').classes()).toContain('min-w-max')
  })

  it('marks sortable headers as focusable with an aria-sort state', () => {
    const wrapper = mountTable()
    const sortable = headerCells(wrapper)[0]
    expect(sortable.attributes('tabindex')).toBe('0')
    // No active sort yet → aria-sort absent.
    expect(sortable.attributes('aria-sort')).toBeUndefined()
    // Non-sortable header is not focusable.
    expect(headerCells(wrapper)[1].attributes('tabindex')).toBeUndefined()
  })

  it('toggles sort on Enter for a sortable header', async () => {
    const wrapper = mountTable()
    const sortable = headerCells(wrapper)[0]
    sortable.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await Promise.resolve()

    // After sorting asc, aria-sort reflects the direction.
    expect(headerCells(wrapper)[0].attributes('aria-sort')).toBe('ascending')
    // Rows are re-ordered: alpha before beta.
    const firstRowName = wrapper.findAll('tbody tr')[0].text()
    expect(firstRowName).toContain('alpha')
  })

  it('toggles sort on Space (prevented) for a sortable header', async () => {
    const wrapper = mountTable()
    const sortable = headerCells(wrapper)[0]
    sortable.element.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }))
    await Promise.resolve()

    expect(headerCells(wrapper)[0].attributes('aria-sort')).toBe('ascending')
  })

  it('flips the sort direction on a second activation', async () => {
    const wrapper = mountTable()
    const sortable = headerCells(wrapper)[0]
    sortable.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await Promise.resolve()
    sortable.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await Promise.resolve()

    expect(headerCells(wrapper)[0].attributes('aria-sort')).toBe('descending')
    const firstName = wrapper.findAll('tbody tr')[0].text()
    expect(firstName).toContain('beta')
  })

  it('gives each mounted table a unique scroll-instructions id', () => {
    const Parent = defineComponent({
      setup() {
        return () => h('div', [
          h(DataTable, { columns, rows }),
          h(DataTable, { columns, rows }),
        ])
      },
    })
    const wrapper = mount(Parent)
    const regions = wrapper.findAll('[aria-describedby^="data-table-scroll-instructions-"]')
    expect(regions).toHaveLength(2)
    const ids = regions.map(region => region.attributes('aria-describedby')!)
    expect(new Set(ids).size).toBe(2)
    for (const id of ids) {
      expect(wrapper.find(`#${id}`).exists()).toBe(true)
    }
  })
})

describe('DataTable horizontal scroll region', () => {
  it('is a keyboard focus stop with a visible ring and claims no interaction it does not have', () => {
    const wrapper = mountTable()
    const scrollRegion = wrapper.get('div.overflow-x-auto')

    expect(scrollRegion.attributes('tabindex')).toBe('0')
    // A11Y-3: a focus stop must be visible.
    expect(scrollRegion.classes()).toContain('focus-visible:ring-2')
    // A static element must not add a role or a handler it does not need —
    // that is the pattern that drew the static-element-interaction warning.
    expect(scrollRegion.attributes('role')).toBeUndefined()
  })

  it('stays reachable when the table has nothing focusable inside it', () => {
    // No sortable header and non-clickable rows: the container is the only
    // route a keyboard user has to the overflowed columns.
    const wrapper = mount(DataTable, {
      props: {
        columns: [{ key: 'value', label: 'Value' }],
        rows: [{ value: 1 }],
        rowClickable: false,
      },
    })

    expect(wrapper.findAll('thead th[tabindex="0"]')).toHaveLength(0)
    expect(wrapper.findAll('tbody tr[tabindex="0"]')).toHaveLength(0)
    expect(wrapper.get('div.overflow-x-auto').attributes('tabindex')).toBe('0')
  })
})

describe('DataTable responsive fit (FAR-2365)', () => {
  const responsiveColumns: Column[] = [
    { key: 'name', label: 'Name', width: '40%' },
    { key: 'value', label: 'Value', width: '10%', hideBelow: 'xl' },
  ]

  it('keeps the existing min-width scroll layout by default', () => {
    const wrapper = mount(DataTable, { props: { columns: responsiveColumns, rows } })
    const table = wrapper.get('table')
    expect(table.classes()).toContain('min-w-max')
    expect(table.classes()).not.toContain('lg:table-fixed')
  })

  it('opt-in fit allows the table to shrink from lg up', () => {
    const wrapper = mount(DataTable, { props: { columns: responsiveColumns, rows, fit: true } })
    const table = wrapper.get('table')
    expect(table.classes()).toContain('table-auto')
    expect(table.classes()).toContain('lg:table-fixed')
    expect(table.classes()).toContain('lg:min-w-0')
  })

  it('applies the declared width to both the header and body cells', () => {
    const wrapper = mount(DataTable, { props: { columns: responsiveColumns, rows, fit: true } })
    expect(wrapper.findAll('thead th')[0].attributes('style')).toContain('width: 40%')
    expect(wrapper.findAll('tbody tr')[0].findAll('td')[0].attributes('style')).toContain('width: 40%')
  })

  it('hides the same column in the header and body below its breakpoint', () => {
    const wrapper = mount(DataTable, { props: { columns: responsiveColumns, rows, fit: true } })
    for (const cell of [wrapper.findAll('thead th')[1], wrapper.findAll('tbody tr')[0].findAll('td')[1]]) {
      expect(cell.classes()).toContain('hidden')
      expect(cell.classes()).toContain('xl:table-cell')
    }
  })

  it('applies widths and breakpoint classes to the loading skeleton cells', () => {
    const loadingColumns: Column[] = [
      { key: 'name', label: 'Name', width: '40%' },
      { key: 'value', label: 'Value', numeric: true, width: '10%', hideBelow: 'xl' },
      { key: 'extra', label: 'Extra' },
    ]
    const wrapper = mount(DataTable, {
      props: { columns: loadingColumns, rows: [], loading: true, fit: true },
    })
    const skeleton = wrapper.findAll('tbody tr')[0].findAll('td')
    expect(skeleton[0].attributes('style')).toContain('width: 40%')
    expect(skeleton[1].classes()).toContain('hidden')
    expect(skeleton[1].classes()).toContain('xl:table-cell')
    expect(skeleton[2].attributes('style')).toBeUndefined()
  })
})
