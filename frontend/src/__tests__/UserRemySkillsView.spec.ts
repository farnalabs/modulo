import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import UserRemySkillsView from '../views/UserRemySkillsView.vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation(() => {
      return Promise.resolve({ data: [], error: null })
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

  it('updates the row to Inactive after a successful toggle (readonly vue-query data fix, FAR-630)', async () => {
    // toggleSkillActive() replaces the whole query array through the writable
    // computed (vue-query data is deep-readonly, so `skillsResp.value[idx] = x`
    // would be silently dropped), so the row reflects the new active state.
    const { api } = await import('../lib/api/client')
    const skill = {
      id: 's1',
      name: 'Search Docs',
      description: null,
      triggers: null,
      body: 'Search the docs.',
      active: true,
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-01T00:00:00Z',
    }
    ;(api.GET as ReturnType<typeof vi.fn>).mockResolvedValue({ data: [skill], error: null })
    ;(api.PUT as ReturnType<typeof vi.fn>).mockResolvedValue({ data: { ...skill, active: false }, error: null })
    const wrapper = mount(UserRemySkillsView)
    await flushPromises()

    expect(wrapper.text()).toContain('Active')
    const toggleBtn = wrapper.findAll('button').find((b) => b.text().trim() === 'Active')
    await toggleBtn!.trigger('click')
    await flushPromises()

    expect(wrapper.findAll('button').some((b) => b.text().trim() === 'Inactive')).toBe(true)
    expect(wrapper.findAll('button').some((b) => b.text().trim() === 'Active')).toBe(false)
    wrapper.unmount()
  })
})
