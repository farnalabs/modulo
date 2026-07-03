import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import RemySkillDialog from '../components/remy/RemySkillDialog.vue'

const dialogStub = { template: '<div><slot /></div>' }

describe('RemySkillDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders create dialog when openCreate is called', async () => {
    const wrapper = mount(RemySkillDialog, {
      global: {
        stubs: {
          Dialog: dialogStub,
          DialogContent: dialogStub,
          DialogDescription: dialogStub,
          DialogFooter: dialogStub,
          DialogHeader: dialogStub,
          DialogTitle: dialogStub,
        },
      },
    })
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()
    expect(wrapper.text()).toContain('Add Skill')
  })

  it('has all form fields', async () => {
    const wrapper = mount(RemySkillDialog, {
      global: {
        stubs: {
          Dialog: dialogStub,
          DialogContent: dialogStub,
          DialogDescription: dialogStub,
          DialogFooter: dialogStub,
          DialogHeader: dialogStub,
          DialogTitle: dialogStub,
        },
      },
    })
    const vm = wrapper.vm as any
    vm.openCreate()
    await nextTick()
    expect(wrapper.find('[data-testid="remy-skills-form-name"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="remy-skills-form-description"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="remy-skills-form-triggers"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="remy-skills-form-body"]').exists()).toBe(true)
  })
})
