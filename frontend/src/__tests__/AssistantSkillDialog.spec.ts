import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Mock } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'
import AssistantSkillDialog from '../components/assistant/AssistantSkillDialog.vue'
import type { SkillFormItem } from '../components/assistant/AssistantSkillDialog.vue'
import { api } from '@/lib/api/client'

vi.mock('@/lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('@/lib/api/formatError', () => ({
  formatApiError: (err: unknown) => {
    if (typeof err === 'object' && err !== null && 'detail' in err) return (err as { detail: string }).detail
    if (typeof err === 'string') return err
    if (err instanceof Error) return err.message
    return 'Unknown error'
  },
}))

const apiPost = api.POST as unknown as Mock
const apiPut = api.PUT as unknown as Mock
const apiDelete = api.DELETE as unknown as Mock

const dialogStub = { template: '<div><slot name="header" /><slot /><slot name="footer" /></div>' }
const buttonStub = { template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>', props: ['disabled'] }

function mountDialog() {
  return mount(AssistantSkillDialog, {
    global: {
      stubs: {
        Dialog: dialogStub,
        DialogContent: dialogStub,
        DialogDescription: dialogStub,
        DialogFooter: dialogStub,
        DialogHeader: dialogStub,
        DialogTitle: dialogStub,
        Button: buttonStub,
      },
    },
  })
}

describe('AssistantSkillDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders create dialog when openCreate is called', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()
    expect(wrapper.text()).toContain('Add Skill')
  })

  it('has all form fields', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()
    expect(wrapper.find('[data-testid="assistant-skills-form-name"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="assistant-skills-form-description"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="assistant-skills-form-triggers"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="assistant-skills-form-body"]').exists()).toBe(true)
  })

  it('renders edit dialog when openEdit is called', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    const skill: SkillFormItem = {
      id: 'skill-1',
      name: 'My Skill',
      description: 'A test skill',
      triggers: ['trigger1', 'trigger2'],
      body: '# Instructions\nDo stuff',
      active: true,
    }
    vm.openEdit(skill)
    await nextTick()
    expect(wrapper.text()).toContain('Edit Skill')
    expect((wrapper.find('[data-testid="assistant-skills-form-name"]').element as HTMLInputElement).value).toBe('My Skill')
    expect((wrapper.find('[data-testid="assistant-skills-form-description"]').element as HTMLTextAreaElement).value).toBe('A test skill')
    expect((wrapper.find('[data-testid="assistant-skills-form-triggers"]').element as HTMLInputElement).value).toBe('trigger1, trigger2')
    expect((wrapper.find('[data-testid="assistant-skills-form-body"]').element as HTMLTextAreaElement).value).toBe('# Instructions\nDo stuff')
    expect((wrapper.find('[data-testid="assistant-skills-form-active"]').element as HTMLInputElement).checked).toBe(true)
  })

  it('populates form with null fields handled gracefully in openEdit', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openEdit({ id: 'skill-2', name: 'Minimal', description: null, triggers: null, body: null, active: false })
    await nextTick()
    expect((wrapper.find('[data-testid="assistant-skills-form-name"]').element as HTMLInputElement).value).toBe('Minimal')
    expect((wrapper.find('[data-testid="assistant-skills-form-description"]').element as HTMLTextAreaElement).value).toBe('')
    expect((wrapper.find('[data-testid="assistant-skills-form-triggers"]').element as HTMLInputElement).value).toBe('')
    expect((wrapper.find('[data-testid="assistant-skills-form-body"]').element as HTMLTextAreaElement).value).toBe('')
    expect((wrapper.find('[data-testid="assistant-skills-form-active"]').element as HTMLInputElement).checked).toBe(false)
  })

  it('shows "Update" submit label in edit mode', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openEdit({ id: 'skill-1', name: 'X', description: null, triggers: null, body: '', active: true })
    await nextTick()
    expect(wrapper.text()).toContain('Update')
  })

  it('shows "Create" submit label in create mode', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()
    expect(wrapper.text()).toContain('Create')
  })

  it('closes form and resets editingId on closeForm', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()
    expect(wrapper.text()).toContain('Add Skill')

    vm.closeForm()
    await nextTick()
    // After close, the form fields should no longer be visible (dialog hidden)
    expect(vm.dialogOpen).toBe(false)
    expect(vm.editingId).toBeNull()
  })

  it('saves a new skill via POST', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    // Fill the form
    const nameInput = wrapper.find('[data-testid="assistant-skills-form-name"]')
    await nameInput.setValue('New Skill')
    const bodyInput = wrapper.find('[data-testid="assistant-skills-form-body"]')
    await bodyInput.setValue('Do things')

    // Submit
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith(
      '/api/v1/admin/assistant/skills',
      expect.objectContaining({
        body: expect.objectContaining({
          name: 'New Skill',
          body: 'Do things',
        }),
      }),
    )
  })

  it('parses comma-separated triggers', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('Skill')
    await wrapper.find('[data-testid="assistant-skills-form-triggers"]').setValue('a, b, c')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith(
      '/api/v1/admin/assistant/skills',
      expect.objectContaining({
        body: expect.objectContaining({
          triggers: ['a', 'b', 'c'],
        }),
      }),
    )
  })

  it('updates an existing skill via PUT', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openEdit({ id: 'skill-1', name: 'Old', description: null, triggers: null, body: '', active: true })
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('Updated')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiPut).toHaveBeenCalledWith(
      '/api/v1/admin/assistant/skills/{skill_id}',
      expect.objectContaining({
        params: { path: { skill_id: 'skill-1' } },
        body: expect.objectContaining({ name: 'Updated' }),
      }),
    )
  })

  it('emits saved on successful create', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('New')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.emitted('saved')!.length).toBeGreaterThanOrEqual(1)
  })

  it('emits saved on successful update', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openEdit({ id: 's-1', name: 'X', description: null, triggers: null, body: '', active: true })
    await nextTick()

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.emitted('saved')!.length).toBeGreaterThanOrEqual(1)
  })

  it('does not save when name is empty', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    // Name is empty by default, submit button should be disabled
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiPost).not.toHaveBeenCalled()
  })

  it('does not save when name is whitespace only', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('   ')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiPost).not.toHaveBeenCalled()
  })

  it('shows error on POST failure', async () => {
    apiPost.mockResolvedValue({ data: null, error: { detail: 'Name already exists' } } as any)

    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('Duplicate')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to create skill')
    expect(wrapper.text()).toContain('Name already exists')
  })

  it('shows error on PUT failure', async () => {
    apiPut.mockResolvedValue({ data: null, error: { detail: 'Not found' } } as any)

    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openEdit({ id: 's-1', name: 'X', description: null, triggers: null, body: '', active: true })
    await nextTick()

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to update skill')
  })

  it('shows error when save throws', async () => {
    apiPost.mockRejectedValue(new Error('Network error'))

    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('Test')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save skill')
    expect(wrapper.text()).toContain('Network error')
  })

  it('disables submit while saving', async () => {
    apiPost.mockReturnValue(new Promise(() => {})) // never resolves

    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('Test')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Saving...')
  })

  it('shows delete dialog when openDelete is called', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openDelete({ id: 'skill-1', name: 'To Delete', description: null, triggers: null, body: '', active: true })
    await nextTick()

    expect(wrapper.text()).toContain('Delete Skill')
    expect(wrapper.text()).toContain('To Delete')
  })

  it('confirms deletion and emits saved', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openDelete({ id: 'skill-1', name: 'To Delete', description: null, triggers: null, body: '', active: true })
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-delete-confirm"]').trigger('click')
    await flushPromises()

    expect(apiDelete).toHaveBeenCalledWith(
      '/api/v1/admin/assistant/skills/{skill_id}',
      expect.objectContaining({
        params: { path: { skill_id: 'skill-1' } },
      }),
    )
    expect(wrapper.emitted('saved')!.length).toBeGreaterThanOrEqual(1)
  })

  it('shows error on delete failure', async () => {
    apiDelete.mockResolvedValue({ data: null, error: { detail: 'Cannot delete' } } as any)

    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openDelete({ id: 'skill-1', name: 'X', description: null, triggers: null, body: '', active: true })
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-delete-confirm"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to delete skill')
  })

  it('shows error when delete throws', async () => {
    apiDelete.mockRejectedValue(new Error('Connection lost'))

    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openDelete({ id: 'skill-1', name: 'X', description: null, triggers: null, body: '', active: true })
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-delete-confirm"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to delete skill')
    expect(wrapper.text()).toContain('Connection lost')
  })

  it('cancels deletion when cancel button is clicked', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openDelete({ id: 'skill-1', name: 'X', description: null, triggers: null, body: '', active: true })
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-delete-cancel"]').trigger('click')
    await flushPromises()

    expect(apiDelete).not.toHaveBeenCalled()
    expect(vm.deleteOpen).toBe(false)
  })

  it('shows "Deleting..." label while delete is in progress', async () => {
    apiDelete.mockReturnValue(new Promise(() => {})) // never resolves

    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openDelete({ id: 'skill-1', name: 'X', description: null, triggers: null, body: '', active: true })
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-delete-confirm"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Deleting...')
  })

  it('disables delete confirm button while deleting', async () => {
    apiDelete.mockReturnValue(new Promise(() => {}))

    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openDelete({ id: 'skill-1', name: 'X', description: null, triggers: null, body: '', active: true })
    await nextTick()

    const confirmBtn = wrapper.find('[data-testid="assistant-skills-delete-confirm"]')
    await confirmBtn.trigger('click')
    await flushPromises()

    expect((confirmBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('renders custom endpoint props', async () => {
    const wrapper = mount(AssistantSkillDialog, {
      props: {
        createEndpoint: '/api/v1/me/assistant/skills',
        updateEndpoint: '/api/v1/me/assistant/skills/{skill_id}',
        deleteEndpoint: '/api/v1/me/assistant/skills/{skill_id}',
      },
      global: {
        stubs: {
          Dialog: dialogStub,
          DialogContent: dialogStub,
          DialogDescription: dialogStub,
          DialogFooter: dialogStub,
          DialogHeader: dialogStub,
          DialogTitle: dialogStub,
          Button: buttonStub,
        },
      },
    })
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('Custom')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith(
      '/api/v1/me/assistant/skills',
      expect.anything(),
    )
  })

  it('clears saveError when opening create dialog', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any

    // First, trigger an error
    apiPost.mockResolvedValue({ data: null, error: { detail: 'fail' } } as any)
    vm.openCreate()
    await nextTick()
    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('X')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to create skill')

    // Now open create again - error should be cleared
    apiPost.mockResolvedValue({ data: null, error: undefined } as any)
    vm.openCreate()
    await nextTick()
    expect(wrapper.text()).not.toContain('Failed to create skill')
  })

  it('active checkbox defaults to true in create mode', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()
    expect((wrapper.find('[data-testid="assistant-skills-form-active"]').element as HTMLInputElement).checked).toBe(true)
  })

  it('submit is disabled when name is empty', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    const submitBtn = wrapper.find('[data-testid="assistant-skills-form-submit"]')
    expect((submitBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('submit is enabled when name is provided', async () => {
    const wrapper = mountDialog()
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()

    await wrapper.find('[data-testid="assistant-skills-form-name"]').setValue('Valid Name')
    await nextTick()

    const submitBtn = wrapper.find('[data-testid="assistant-skills-form-submit"]')
    expect((submitBtn.element as HTMLButtonElement).disabled).toBe(false)
  })
})
