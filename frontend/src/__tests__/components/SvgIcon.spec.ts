import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SvgIcon from '../../components/SvgIcon.vue'
import { getNavGroups } from '../../config/navigation'

describe('SvgIcon', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  // This test scans the entire manifest icon registry: duration scales with
  // the icon/route count and the Vite transform cache state — ~10s isolated,
  // 20s+ under full-suite load (FAR-632), 15s+ cold on Windows (FAR-639) —
  // so zero margin at the 15s global default caused flakes. Give this scan a
  // wide 120s budget (same mechanism as app-bootstrap.spec.ts).
  it('registers every icon referenced by manifest navigation', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const iconNames = new Set(
      getNavGroups().flatMap((group) => group.items.map((item) => item.icon)),
    )

    for (const name of iconNames) {
      mount(SvgIcon, { props: { name } }).unmount()
    }

    expect(warnSpy).not.toHaveBeenCalled()
  }, 120_000)

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
