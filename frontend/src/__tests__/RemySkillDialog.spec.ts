import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import RemySkillDialog from '../components/remy/RemySkillDialog.vue'

describe('RemySkillDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders create dialog when openCreate is called', async () => {
    const wrapper = mount(RemySkillDialog)
    const vm = wrapper.vm as any
    vm.openCreate()
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('Add Skill')
  })

  it('has all form fields', async () => {
    const wrapper = mount(RemySkillDialog)
    const vm = wrapper.vm as any
    vm.openCreate()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="remy-skills-form-name"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="remy-skills-form-description"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="remy-skills-form-triggers"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="remy-skills-form-body"]').exists()).toBe(true)
  })
})
