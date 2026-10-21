import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TableSkeleton from '../../components/shared/TableSkeleton.vue'

describe('TableSkeleton', () => {
  it('announces loading without burying the live region under aria-busy', () => {
    const wrapper = mount(TableSkeleton, {
      props: { columns: 2, rows: 1, ariaLabel: 'Loading runs' },
    })

    const live = wrapper.find('output')
    expect(live.exists()).toBe(true)
    expect(live.attributes('aria-live')).toBe('polite')
    expect(live.text()).toBe('Loading runs')

    // A busy ancestor defers the live-region announcement until it clears —
    // and this skeleton unmounts rather than clearing busy. The placeholder
    // table is the thing that is actually busy.
    expect(live.element.closest('[aria-busy="true"]')).toBeNull()
    expect(wrapper.find('table').attributes('aria-busy')).toBe('true')
  })
})
