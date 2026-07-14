import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SvgIcon from '../../components/SvgIcon.vue'
import { getNavGroups } from '../../config/navigation'

describe('SvgIcon', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('registers every icon referenced by manifest navigation', () => {
    const warn = vi.spyOn(console, 'warn')
    const iconNames = new Set(
      getNavGroups().flatMap((group) => group.items.map((item) => item.icon)),
    )

    for (const name of iconNames) {
      mount(SvgIcon, { props: { name } }).unmount()
    }

    expect(warn).not.toHaveBeenCalled()
  })

  it('renders the canonical Lucide circle-play icon for runs', () => {
    const wrapper = mount(SvgIcon, { props: { name: 'CirclePlay' } })

    expect(wrapper.find('svg').classes()).toContain('lucide-circle-play')
    expect(wrapper.find('circle').exists()).toBe(true)
    expect(wrapper.find('path').exists()).toBe(true)
  })
})
