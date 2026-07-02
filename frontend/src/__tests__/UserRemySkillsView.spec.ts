import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import UserRemySkillsView from '../views/UserRemySkillsView.vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation(() => {
      return Promise.resolve({ data: { items: [] }, error: null })
    }),
    PUT: vi.fn().mockImplementation(() => {
      return Promise.resolve({ data: null, error: null })
    }),
    POST: vi.fn().mockImplementation(() => {
      return Promise.resolve({ data: null, error: null })
    }),
    DELETE: vi.fn().mockImplementation(() => {
      return Promise.resolve({ data: null, error: null })
    }),
    PATCH: vi.fn().mockImplementation(() => {
      return Promise.resolve({ data: null, error: null })
    }),
  },
}))

describe('UserRemySkillsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders the skills page title', async () => {
    const wrapper = mount(UserRemySkillsView)
    await flushPromises()
    expect(wrapper.text()).toContain('My Remy Skills')
  })

  it('renders the add skill button', async () => {
    const wrapper = mount(UserRemySkillsView)
    await flushPromises()
    const btn = wrapper.find('[data-testid="remy-user-skills-add"]')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toContain('Add Skill')
  })
})
