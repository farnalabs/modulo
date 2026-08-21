import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SvgIcon from '../../components/SvgIcon.vue'
import { getNavGroups } from '../../config/navigation'

describe('SvgIcon', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('registers every icon referenced by manifest navigation', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const iconNames = new Set(
      getNavGroups().flatMap((group) => group.items.map((item) => item.icon)),
    )

    for (const name of iconNames) {
      mount(SvgIcon, { props: { name } }).unmount()
    }

    expect(warnSpy).not.toHaveBeenCalled()
  })

  it('renders the canonical Lucide circle-play icon for runs', () => {
    const wrapper = mount(SvgIcon, { props: { name: 'CirclePlay' } })

    expect(wrapper.find('svg').classes()).toContain('lucide-circle-play')
    expect(wrapper.find('circle').exists()).toBe(true)
    expect(wrapper.find('path').exists()).toBe(true)
  })

  it('falls back to the File icon for unknown names instead of rendering empty', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const wrapper = mount(SvgIcon, { props: { name: 'DefinitelyNotAnIcon' } })

    expect(warnSpy).toHaveBeenCalledWith('SvgIcon: unknown icon "DefinitelyNotAnIcon"')
    expect(wrapper.find('svg').exists()).toBe(true)
    expect(wrapper.find('svg').classes()).toContain('lucide-file')
  })

  it('renders the housekeeping icon for the Broom name (Sparkles substitution)', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const wrapper = mount(SvgIcon, { props: { name: 'Broom' } })

    expect(warnSpy).not.toHaveBeenCalled()
    expect(wrapper.find('svg').exists()).toBe(true)
  })
})
