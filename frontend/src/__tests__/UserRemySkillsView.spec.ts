import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import UserRemySkillsView from '../views/UserRemySkillsView.vue'

describe('UserRemySkillsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders the skills page title', () => {
    const wrapper = mount(UserRemySkillsView)
    expect(wrapper.text()).toContain('My Remy Skills')
  })

  it('renders the add skill button', () => {
    const wrapper = mount(UserRemySkillsView)
    const btn = wrapper.find('[data-testid="remy-user-skills-add"]')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toContain('Add Skill')
  })
})
