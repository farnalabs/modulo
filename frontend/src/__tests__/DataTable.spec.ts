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
