import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import type { UserSkill } from '../../types/remy'

const { apiGet, apiPost, apiPut, apiDelete } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}))

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
  api: {
    GET: apiGet,
    POST: apiPost,
    PUT: apiPut,
    PATCH: vi.fn(),
    DELETE: apiDelete,
  },
}))

import RemySkillManager from '../../components/remy/RemySkillManager.vue'
import { useRemyStore } from '../../composables/useRemyStore'

function makeSkill(overrides: Partial<UserSkill> = {}): UserSkill {
  return {
    id: 'skill-1',
    name: 'Code Review',
    description: 'Reviews code changes',
    triggers: ['review', 'cr'],
    body: 'Do a careful review',
    active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function mountManager() {
  // Relies on setActivePinia in beforeEach — the same pinia the test observes.
  return mount(RemySkillManager)
}

describe('RemySkillManager', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    apiGet.mockResolvedValue({ data: [], error: undefined })
    apiPost.mockResolvedValue({ data: makeSkill(), error: undefined })
    apiPut.mockResolvedValue({ data: makeSkill(), error: undefined })
    apiDelete.mockResolvedValue({ data: null, error: undefined })
  })

  it('shows the empty state when no skills exist', async () => {
    const wrapper = await flushMount()
    expect(wrapper.text()).toContain('No skills yet')
    expect(wrapper.find('.remy-skill-item').exists()).toBe(false)
  })

  it('renders fetched skills with name, description and trigger tags', async () => {
    apiGet.mockResolvedValue({ data: [makeSkill()], error: undefined })
    const wrapper = await flushMount()
    expect(wrapper.text()).toContain('Code Review')
    expect(wrapper.text()).toContain('Reviews code changes')
    expect(wrapper.findAll('.remy-trigger-tag').map(t => t.text())).toEqual(['review', 'cr'])
  })

  it('surfaces a fetch error inline', async () => {
    apiGet.mockResolvedValue({ data: null, error: { detail: 'boom' } })
    const wrapper = await flushMount()
    expect(wrapper.text()).toContain('Failed to load skills: boom')
  })

  it('surfaces a thrown fetch error inline', async () => {
    apiGet.mockRejectedValue(new Error('network down'))
    const wrapper = await flushMount()
    expect(wrapper.text()).toContain('network down')
  })

  it('creates a skill and refreshes the list', async () => {
    apiGet.mockResolvedValue({ data: [makeSkill()], error: undefined })
    const store = useRemyStore()
    const signalSpy = vi.spyOn(store, 'signalSkillsChanged')
    const wrapper = await flushMount()
    await wrapper.find('button[title="New skill"]').trigger('click')
    expect(wrapper.find('#remyskill-name-input').exists()).toBe(true)
    await wrapper.find('#remyskill-name-input').setValue('New Skill')
    await wrapper.find('#remyskill-description-input').setValue('Does things')
    await wrapper.find('#remyskill-triggers-input').setValue('run, deploy')
    await wrapper.find('textarea.remy-skill-textarea').setValue('Body text')
    const createBtn = wrapper.findAll('button').find(b => b.text() === 'Create')
    expect(createBtn).toBeDefined()
    await createBtn!.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/v1/me/remy/skills', {
      body: {
        name: 'New Skill',
        description: 'Does things',
        triggers: ['run', 'deploy'],
        body: 'Body text',
        active: true,
      },
    })
    expect(signalSpy).toHaveBeenCalled()
    expect(wrapper.find('.remy-skill-form').exists()).toBe(false)
  })

  it('blocks saving a skill without a name', async () => {
    const wrapper = await flushMount()
    await wrapper.find('button[title="New skill"]').trigger('click')
    const createBtn = wrapper.findAll('button').find(b => b.text() === 'Create')
    expect(createBtn!.attributes('disabled')).toBeDefined()
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('surfaces a create error inline and keeps the form open', async () => {
    apiPost.mockResolvedValue({ data: null, error: { detail: 'duplicate name' } })
    const wrapper = await flushMount()
    await wrapper.find('button[title="New skill"]').trigger('click')
    await wrapper.find('#remyskill-name-input').setValue('Dup')
    const createBtn = wrapper.findAll('button').find(b => b.text() === 'Create')
    await createBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to create skill: duplicate name')
    expect(wrapper.find('.remy-skill-form').exists()).toBe(true)
  })

  it('edits an existing skill and updates it via PUT', async () => {
    apiGet.mockResolvedValue({ data: [makeSkill()], error: undefined })
    const wrapper = await flushMount()
    await wrapper.find('button[aria-label="Edit"]').trigger('click')
    const nameInput = wrapper.find('#remyskill-name-input')
    expect((nameInput.element as HTMLInputElement).value).toBe('Code Review')
    expect((wrapper.find('#remyskill-triggers-input').element as HTMLInputElement).value).toBe('review, cr')
    await nameInput.setValue('Deep Review')
    const updateBtn = wrapper.findAll('button').find(b => b.text() === 'Update')
    await updateBtn!.trigger('click')
    await flushPromises()
    expect(apiPut).toHaveBeenCalledWith('/api/v1/me/remy/skills/{skill_id}', {
      params: { path: { skill_id: 'skill-1' } },
      body: expect.objectContaining({ name: 'Deep Review', triggers: ['review', 'cr'] }),
    })
    expect(wrapper.find('.remy-skill-form').exists()).toBe(false)
  })

  it('surfaces an update error inline', async () => {
    apiGet.mockResolvedValue({ data: [makeSkill()], error: undefined })
    apiPut.mockResolvedValue({ data: null, error: { detail: 'stale version' } })
    const wrapper = await flushMount()
    await wrapper.find('button[aria-label="Edit"]').trigger('click')
    const updateBtn = wrapper.findAll('button').find(b => b.text() === 'Update')
    await updateBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to update skill: stale version')
  })

  it('deletes a skill and signals the store', async () => {
    apiGet.mockResolvedValue({ data: [makeSkill()], error: undefined })
    const store = useRemyStore()
    const signalSpy = vi.spyOn(store, 'signalSkillsChanged')
    const wrapper = await flushMount()
    expect(wrapper.find('.remy-skill-item').exists()).toBe(true)
    await wrapper.find('button[aria-label="Delete"]').trigger('click')
    await flushPromises()
    expect(apiDelete).toHaveBeenCalledWith('/api/v1/me/remy/skills/{skill_id}', {
      params: { path: { skill_id: 'skill-1' } },
    })
    expect(signalSpy).toHaveBeenCalled()
    expect(wrapper.find('.remy-skill-item').exists()).toBe(false)
  })

  it('surfaces a delete error inline and keeps the skill', async () => {
    apiGet.mockResolvedValue({ data: [makeSkill()], error: undefined })
    apiDelete.mockResolvedValue({ data: null, error: { detail: 'locked' } })
    const wrapper = await flushMount()
    await wrapper.find('button[aria-label="Delete"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to delete skill: locked')
    expect(wrapper.find('.remy-skill-item').exists()).toBe(true)
  })

  it('cancels the form without saving', async () => {
    const wrapper = await flushMount()
    await wrapper.find('button[title="New skill"]').trigger('click')
    expect(wrapper.find('.remy-skill-form').exists()).toBe(true)
    const cancelBtn = wrapper.findAll('button').find(b => b.text() === 'Cancel')
    await cancelBtn!.trigger('click')
    expect(wrapper.find('.remy-skill-form').exists()).toBe(false)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('hides the new-skill button while the form is open', async () => {
    const wrapper = await flushMount()
    await wrapper.find('button[title="New skill"]').trigger('click')
    expect(wrapper.find('button[title="New skill"]').exists()).toBe(false)
  })

  async function flushMount() {
    const wrapper = mountManager()
    await flushPromises()
    return wrapper
  }
})
