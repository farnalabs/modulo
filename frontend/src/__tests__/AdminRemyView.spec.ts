import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import AdminRemyView from '../views/AdminRemyView.vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation(() => {
      return Promise.resolve({ data: null, error: null })
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

describe('AdminRemyView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders the config page title', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    expect(wrapper.text()).toContain('Remy Configuration')
  })

  it('renders the system prompt section', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    expect(wrapper.text()).toContain('System Prompt')
  })

  it('renders the skills section', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    expect(wrapper.text()).toContain('Skills')
  })

  it('renders access list section', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    expect(wrapper.text()).toContain('Access List')
  })

  it('renders configured providers section', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    expect(wrapper.text()).toContain('Configured Providers')
  })
})
