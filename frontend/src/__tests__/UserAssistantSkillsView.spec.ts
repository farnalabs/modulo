import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import UserAssistantSkillsView from '../views/UserAssistantSkillsView.vue'

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

describe('UserAssistantSkillsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders the skills page title', async () => {
    const wrapper = mount(UserAssistantSkillsView)
    await flushPromises()
    expect(wrapper.text()).toContain('My Assistant Skills')
  })

  it('renders the add skill button', async () => {
    const wrapper = mount(UserAssistantSkillsView)
    await flushPromises()
    const btn = wrapper.find('[data-testid="assistant-user-skills-add"]')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toContain('Add Skill')
  })

  it('renders empty descriptions and trigger placeholders, and opens edit/delete dialogs', async () => {
    const { api } = await import('../lib/api/client')
    const skill = {
      id: 's-empty',
      name: 'Empty skill',
      description: '',
      triggers: [],
      body: 'Body',
      active: false,
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-01T00:00:00Z',
    }
    ;(api.GET as ReturnType<typeof vi.fn>).mockResolvedValue({ data: [skill], error: null })
    const wrapper = mount(UserAssistantSkillsView)
    await flushPromises()

    expect(wrapper.find('[data-testid="assistant-user-skill-row-s-empty"]').text()).toContain('—')
    await wrapper.find('[data-testid="assistant-user-skill-edit-s-empty"]').trigger('click')
    await wrapper.find('[data-testid="assistant-user-skill-delete-s-empty"]').trigger('click')
    expect(wrapper.exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a formatted API error when toggling fails', async () => {
    const { api } = await import('../lib/api/client')
    const skill = {
      id: 's-error', name: 'Error skill', description: null, triggers: null,
      body: 'Body', active: true, created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z',
    }
    ;(api.GET as ReturnType<typeof vi.fn>).mockResolvedValue({ data: [skill], error: null })
    ;(api.PUT as ReturnType<typeof vi.fn>).mockResolvedValue({ data: null, error: { detail: 'toggle denied' } })
    const wrapper = mount(UserAssistantSkillsView)
    await flushPromises()

    await wrapper.find('[data-testid="assistant-user-skill-toggle-s-error"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to toggle skill')
    expect(wrapper.find('[data-testid="assistant-user-skill-toggle-s-error"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('shows a formatted error when toggling throws', async () => {
    const { api } = await import('../lib/api/client')
    const skill = {
      id: 's-throw', name: 'Throw skill', description: null, triggers: null,
      body: 'Body', active: true, created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z',
    }
    ;(api.GET as ReturnType<typeof vi.fn>).mockResolvedValue({ data: [skill], error: null })
    ;(api.PUT as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network down'))
    const wrapper = mount(UserAssistantSkillsView)
    await flushPromises()

    await wrapper.find('[data-testid="assistant-user-skill-toggle-s-throw"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to toggle skill')
    wrapper.unmount()
  })

  it('ignores a toggle request while the same skill is already toggling', async () => {
    const { api } = await import('../lib/api/client')
    const skill = {
      id: 's-busy', name: 'Busy skill', description: null, triggers: null,
      body: 'Body', active: true, created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z',
    }
    ;(api.GET as ReturnType<typeof vi.fn>).mockResolvedValue({ data: [skill], error: null })
    ;(api.PUT as ReturnType<typeof vi.fn>).mockClear()
    let resolvePut: ((value: unknown) => void) | undefined
    ;(api.PUT as ReturnType<typeof vi.fn>).mockReturnValue(new Promise((resolve) => { resolvePut = resolve }))
    const wrapper = mount(UserAssistantSkillsView)
    await flushPromises()
    const toggle = wrapper.find('[data-testid="assistant-user-skill-toggle-s-busy"]')
    await toggle.trigger('click')
    await toggle.trigger('click')
    expect(api.PUT).toHaveBeenCalledTimes(1)
    resolvePut?.({ data: null, error: null })
    await flushPromises()
    wrapper.unmount()
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
    const wrapper = mount(UserAssistantSkillsView)
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
