import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RunErrorTag from '../components/shared/RunErrorTag.vue'

describe('RunErrorTag', () => {
  it('renders the i18n label for a known dotted code', () => {
    const wrapper = mount(RunErrorTag, { props: { code: 'agent.stall' } })
    expect(wrapper.text()).toBe('Agent stalled')
  })

  it('falls back to Unknown error for an unknown code', () => {
    const wrapper = mount(RunErrorTag, { props: { code: 'some.mystery' } })
    expect(wrapper.text()).toBe('Unknown error')
  })

  it('renders an em dash when code is falsy', () => {
    const wrapper = mount(RunErrorTag, { props: { code: null } })
    expect(wrapper.text()).toBe('—')
  })

  it('applies the class for the code class group (agent destructive vs capacity primary)', () => {
    const agent = mount(RunErrorTag, { props: { code: 'agent.failed' } })
    expect(agent.classes()).toContain('text-destructive')
    const capacity = mount(RunErrorTag, { props: { code: 'capacity.org' } })
    expect(capacity.classes()).toContain('text-primary')
  })

  it('sets the detail title tooltip', () => {
    const wrapper = mount(RunErrorTag, { props: { code: 'node.timeout', detail: 'hit the timeout guard' } })
    expect(wrapper.attributes('title')).toBe('hit the timeout guard')
  })

  it('renders provider codes with a warning-style pill and their i18n label', () => {
    const wrapper = mount(RunErrorTag, { props: { code: 'provider.rate_limited' } })
    expect(wrapper.text()).toBe('Provider rate limited')
    expect(wrapper.classes()).toContain('text-warning')
    const auth = mount(RunErrorTag, { props: { code: 'provider.authentication' } })
    expect(auth.text()).toBe('Authentication failed')
  })
})
