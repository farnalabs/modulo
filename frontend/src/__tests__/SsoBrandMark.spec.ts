import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'

import SsoBrandMark from '../components/SsoBrandMark.vue'

describe('SsoBrandMark', () => {
  it('renders a brand mark for each known preset', () => {
    for (const preset of ['google', 'auth0', 'okta', 'azure-ad', 'onelogin']) {
      const wrapper = mount(SsoBrandMark, { props: { preset } })
      expect(wrapper.find('svg').exists()).toBe(true)
    }
  })

  it('renders a decorative lock icon for the custom/unknown fallback', () => {
    const wrapper = mount(SsoBrandMark, { props: { preset: 'custom' } })
    const svg = wrapper.find('svg')
    expect(svg.exists()).toBe(true)
    expect(svg.attributes('aria-hidden')).toBe('true')
  })
})
