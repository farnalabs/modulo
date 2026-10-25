import { describe, it, expect } from 'vitest'
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
  it('marks sortable headers with a focusable button and an aria-sort state', () => {
    const wrapper = mountTable()
    const sortable = headerCells(wrapper)[0]
    // A real button is the keyboard affordance, not a click-only <th>.
    const sortButton = sortable.find('button')
    expect(sortButton.exists()).toBe(true)
    expect(sortButton.attributes('tabindex')).toBeUndefined()
    // Sortable but not yet sorted → aria-sort="none" (never a missing state).
    expect(sortable.attributes('aria-sort')).toBe('none')
    // Non-sortable header has no sort button and is not focusable.
    const nonSortable = headerCells(wrapper)[1]
    expect(nonSortable.find('button').exists()).toBe(false)
    expect(nonSortable.attributes('tabindex')).toBeUndefined()
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
})
