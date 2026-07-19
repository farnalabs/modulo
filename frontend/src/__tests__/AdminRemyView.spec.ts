import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { setActivePinia, createPinia } from 'pinia'
import AdminRemyView from '../views/AdminRemyView.vue'

vi.mock('../components/FeatureGate.vue', () => ({
  default: { template: '<div><slot /></div>' },
}))

const mockRemyConfig = {
  access_list: { user_ids: [], team_ids: [], org_roles: ['admin'] },
  default_provider: 'anthropic',
  default_model: '',
  default_context_window: 200000,
  allowed_providers: ['anthropic'],
  allowed_models: [],
  system_prompt: '',
  additional_guidance: '',
  permission_mode: 'safe',
  tool_permissions: {},
  rate_limit_max_actions: 30,
  rate_limit_window_seconds: 60,
  auto_execute_threshold: 0.8,
  nogo_page_patterns: [],
  nogo_selector_patterns: [],
  allowed_selectors: [],
  allowed_page_patterns: [],
}

const mockAvailableProviders = {
  native: [
    { id: 'anthropic', label: 'Anthropic' },
    { id: 'openai', label: 'OpenAI' },
  ],
  customTypes: [],
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((path: string) => {
      if (path === '/api/v1/admin/remy/config') {
        return Promise.resolve({ data: mockRemyConfig, error: undefined })
      }
      if (path === '/api/v1/admin/remy/available-providers') {
        return Promise.resolve({ data: mockAvailableProviders, error: undefined })
      }
      if (path === '/api/v1/admin/remy/context-sources') {
        return Promise.resolve({ data: { product_primer: 'always_on', page_context: 'always_on', user_profile: 'always_on' }, error: undefined })
      }
      if (path === '/api/v1/admin/remy/skills') {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      if (path === '/api/v1/model-backends') {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      if (path.includes('/api/v1/admin/users') || path.includes('/api/v1/admin/teams')) {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
}))

describe('AdminRemyView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders the config page title', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Remy Configuration')
  })

  it('renders the system prompt section', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('System Prompt')
  })

  it('renders the skills section', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Skills')
  })

  it('renders access list section', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Access List')
  })

  it('renders configured providers section', async () => {
    const wrapper = mount(AdminRemyView)
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Configured Providers')
  })
})
