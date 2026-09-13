import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import DataTable from '../../../../components/ui/data-table/DataTable.vue'
import type { Column, DataTableRow } from '../../../../components/ui/data-table/DataTable.vue'

const columns: Column[] = [
  { key: 'name', label: 'Name', sortable: true },
  { key: 'count', label: 'Count', sortable: false, numeric: true },
]

const rows: DataTableRow[] = [
  { name: 'Alpha', count: 3 },
  { name: 'Beta', count: 1 },
]

describe('DataTable — keyboard sorting (FAR-821 a11y)', () => {
  it('sorts on Enter and Space for a sortable column header', async () => {
    const wrapper = mount(DataTable, { props: { columns, rows } })
    const sortableHeader = wrapper.find('th[tabindex="0"]')
    expect(sortableHeader.exists()).toBe(true)

    // Initially unsorted.
    expect(sortableHeader.attributes('aria-sort')).toBeUndefined()

    await sortableHeader.trigger('keydown', { key: 'Enter' })
    expect(sortableHeader.attributes('aria-sort')).toBe('ascending')

    await sortableHeader.trigger('keydown', { key: ' ', code: 'Space' })
    expect(sortableHeader.attributes('aria-sort')).toBe('descending')
  })

  it('exposes a tab stop only on sortable headers', () => {
    const wrapper = mount(DataTable, { props: { columns, rows } })
    const headers = wrapper.findAll('th')
    const tabbable = headers.filter((h) => h.attributes('tabindex') === '0')
    expect(tabbable).toHaveLength(1)
  })
})
