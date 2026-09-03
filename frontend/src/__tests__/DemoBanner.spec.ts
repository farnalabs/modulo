import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import DemoBanner from '../components/DemoBanner.vue'
import { setDemoSession } from '../lib/api/auth'
import enUS from '../locales/en-US.js'

const bannerTestId = '[data-testid="demo-banner"]'

describe('DemoBanner', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('renders with role=status when the session is a demo session', () => {
    setDemoSession(true)
    const wrapper = mount(DemoBanner)
    const banner = wrapper.find(bannerTestId)
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('role')).toBe('status')
  })

  it('renders nothing for a normal (non-demo) session', () => {
    const wrapper = mount(DemoBanner)
    expect(wrapper.find(bannerTestId).exists()).toBe(false)
  })

  it('renders the banner strings from the en-US locale keys', () => {
    setDemoSession(true)
    const wrapper = mount(DemoBanner)
    const components = enUS.components as { DemoBanner: { title: string; subtitle: string } }
    expect(wrapper.text()).toContain(components.DemoBanner.title)
    expect(wrapper.text()).toContain(components.DemoBanner.subtitle)
  })
})
