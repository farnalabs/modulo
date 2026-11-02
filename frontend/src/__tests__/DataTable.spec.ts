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
