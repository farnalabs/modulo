import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { usePlanStore } from '../stores/planStore'
import AdminRemyView from '../views/AdminRemyView.vue'

describe('AdminRemyView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = usePlanStore()
    store.$patch({ features: { remy: true }, plan: { tier: 'enterprise' } })
  })

  it('renders the config page title', () => {
    const wrapper = mount(AdminRemyView)
    expect(wrapper.text()).toContain('Remy Configuration')
  })

  it('renders the system prompt section', () => {
    const wrapper = mount(AdminRemyView)
    expect(wrapper.text()).toContain('System Prompt')
  })

  it('renders the skills section', () => {
    const wrapper = mount(AdminRemyView)
    expect(wrapper.text()).toContain('Skills')
  })

  it('renders access list section', () => {
    const wrapper = mount(AdminRemyView)
    expect(wrapper.text()).toContain('Access List')
  })
})
