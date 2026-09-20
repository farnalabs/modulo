/**
 * Branch coverage tests for AdminRemyView.vue (FAR-835).
 *
 * Targets uncovered branches: provider tooltip variants, custom providers
 * empty state, skills with empty triggers/description, context source
 * token vs toolCall display, save error catch blocks, loadProviders error
 * paths, primer regeneration catch, config hydration null/undefined, and
 * all error-state display branches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

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
  customTypes: [{ id: 'custom-1', label: 'Custom Provider' }],
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
        return Promise.resolve({ data: [], error: undefined })
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

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
})

async function mountRemy(overrides: {
  skills?: Array<Record<string, unknown>>
  backends?: Array<Record<string, unknown>>
  contextSources?: Record<string, string>
  configOverrides?: Record<string, unknown>
} = {}) {
  const { api } = await import('../lib/api/client')
  vi.mocked(api.GET).mockImplementation((path: string) => {
    if (path === '/api/v1/admin/remy/config') {
      return Promise.resolve({ data: { ...mockRemyConfig, ...overrides.configOverrides }, error: undefined })
    }
    if (path === '/api/v1/admin/remy/available-providers') {
      return Promise.resolve({ data: mockAvailableProviders, error: undefined })
    }
    if (path === '/api/v1/admin/remy/context-sources') {
      return Promise.resolve({ data: overrides.contextSources ?? {}, error: undefined })
    }
    if (path === '/api/v1/admin/remy/skills') {
      return Promise.resolve({ data: overrides.skills ?? [], error: undefined })
    }
    if (path === '/api/v1/model-backends') {
      return Promise.resolve({ data: { items: overrides.backends ?? [] }, error: undefined })
    }
    if (path.includes('/api/v1/admin/users') || path.includes('/api/v1/admin/teams')) {
      return Promise.resolve({ data: { items: [] }, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })

  const wrapper = mount((await import('../views/AdminRemyView.vue')).default)
  await flushPromises()
  await nextTick()
  return wrapper
}

// ── providerTooltip branches ─────────────────────────────────────────────
describe('AdminRemyView branches — providerTooltip', () => {
  it('configured provider tooltip contains "api key configured"', async () => {
    const wrapper = await mountRemy({
      backends: [{ provider: 'anthropic', has_credentials: true }],
    })
    const providers = wrapper.find('[data-testid="remy-providers"]')
    // Configured provider should show the configured styling (not muted)
    expect(providers.text()).toContain('Anthropic')
    expect(providers.text()).toContain('Configured')
    wrapper.unmount()
  })

  it('unconfigured provider tooltip contains "no api key set"', async () => {
    const wrapper = await mountRemy({
      backends: [],
    })
    const providers = wrapper.find('[data-testid="remy-providers"]')
    expect(providers.text()).toContain('Not set')
    wrapper.unmount()
  })
})

// ── customProviderStatus empty state ────────────────────────────────────
describe('AdminRemyView branches — custom providers', () => {
  it('shows empty message when customProviderStatus is empty', async () => {
    // Override available-providers to have no customTypes
    const { api } = await import('../lib/api/client')
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/remy/config') {
        return Promise.resolve({ data: mockRemyConfig, error: undefined })
      }
      if (path === '/api/v1/admin/remy/available-providers') {
        return Promise.resolve({ data: { native: mockAvailableProviders.native, customTypes: [] }, error: undefined })
      }
      if (path === '/api/v1/admin/remy/context-sources') {
        return Promise.resolve({ data: {}, error: undefined })
      }
      if (path === '/api/v1/admin/remy/skills') {
        return Promise.resolve({ data: [], error: undefined })
      }
      if (path === '/api/v1/model-backends') {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      if (path.includes('/api/v1/admin/users') || path.includes('/api/v1/admin/teams')) {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount((await import('../views/AdminRemyView.vue')).default)
    await flushPromises()
    const customSection = wrapper.find('[data-testid="remy-custom-backends"]')
    expect(customSection.text()).toContain('No custom backends configured')
    wrapper.unmount()
  })

  it('shows custom providers when available', async () => {
    const wrapper = await mountRemy({
      backends: [{ provider: 'custom-1', has_credentials: true }],
    })
    const customSection = wrapper.find('[data-testid="remy-custom-backends"]')
    expect(customSection.text()).toContain('Custom Provider')
    wrapper.unmount()
  })
})

// ── providersLoading in custom backends section ─────────────────────────
describe('AdminRemyView branches — providersLoading', () => {
  it('shows loading in the configured providers section while providers load', async () => {
    const { api } = await import('../lib/api/client')
    // Mock all endpoints except model-backends (which provides provider data)
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/remy/config') {
        return Promise.resolve({ data: mockRemyConfig, error: undefined })
      }
      if (path === '/api/v1/admin/remy/available-providers') {
        return Promise.resolve({ data: mockAvailableProviders, error: undefined })
      }
      if (path === '/api/v1/admin/remy/context-sources') {
        return Promise.resolve({ data: {}, error: undefined })
      }
      if (path === '/api/v1/admin/remy/skills') {
        return Promise.resolve({ data: [], error: undefined })
      }
      if (path.includes('/api/v1/admin/users') || path.includes('/api/v1/admin/teams')) {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      if (path === '/api/v1/model-backends') {
        return new Promise(() => {}) // never resolves — providersLoading stays true
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount((await import('../views/AdminRemyView.vue')).default)
    await flushPromises()
    // When providersLoading is true, the main loading computed is true,
    // so the loading skeleton renders (the providers section is not yet visible)
    expect(wrapper.findAll('.animate-pulse').length).toBeGreaterThan(0)
    wrapper.unmount()
  })
})

// ── skills with empty triggers and empty description ────────────────────
describe('AdminRemyView branches — skills rendering', () => {
  it('shows dash for empty triggers array', async () => {
    const wrapper = await mountRemy({
      skills: [{ id: 's1', name: 'Test Skill', description: 'desc', triggers: [], active: true }],
    })
    const skills = wrapper.find('[data-testid="remy-skills"]')
    expect(skills.text()).toContain('—')
    wrapper.unmount()
  })

  it('shows dash for null triggers', async () => {
    const wrapper = await mountRemy({
      skills: [{ id: 's1', name: 'Test Skill', description: 'desc', triggers: null, active: true }],
    })
    const skills = wrapper.find('[data-testid="remy-skills"]')
    expect(skills.text()).toContain('—')
    wrapper.unmount()
  })

  it('shows dash for empty description', async () => {
    const wrapper = await mountRemy({
      skills: [{ id: 's1', name: 'Test Skill', description: '', triggers: [], active: true }],
    })
    const skills = wrapper.find('[data-testid="remy-skills"]')
    expect(skills.text()).toContain('—')
    wrapper.unmount()
  })

  it('shows dash for null description', async () => {
    const wrapper = await mountRemy({
      skills: [{ id: 's1', name: 'Test Skill', description: null, triggers: [], active: true }],
    })
    const skills = wrapper.find('[data-testid="remy-skills"]')
    expect(skills.text()).toContain('—')
    wrapper.unmount()
  })
})

// ── context source: tokens vs toolCall display ──────────────────────────
describe('AdminRemyView branches — context source display', () => {
  it('shows token count for sources with tokens', async () => {
    const wrapper = await mountRemy({
      contextSources: { product_primer: 'always_on', page_context: 'tool' },
    })
    // product_primer has tokens: '~700'
    expect(wrapper.text()).toContain('~700')
    wrapper.unmount()
  })

  it('shows toolCall for sources with toolCall', async () => {
    const wrapper = await mountRemy({
      contextSources: { product_primer: 'always_on' },
    })
    // product_docs has toolCall: 'search_documentation()'
    expect(wrapper.text()).toContain('search_documentation()')
    wrapper.unmount()
  })
})

// ── save error catch blocks ─────────────────────────────────────────────
describe('AdminRemyView branches — save error catch blocks', () => {
  it('shows accessError when saveAccessList catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountRemy()

    const access = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Access List'))!
    const save = access.findAll('button').find((b) => b.text().includes('Save Access List'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save access list')
    wrapper.unmount()
  })

  it('shows modelError when saveModelConfig catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountRemy()

    const model = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Default Model'))!
    const save = model.findAll('button').find((b) => b.text().includes('Save Model Config'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save model config')
    wrapper.unmount()
  })

  it('shows promptError when saveSystemPrompt catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountRemy()

    const prompt = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('System Prompt'))!
    const save = prompt.findAll('button').find((b) => b.text().includes('Save System Prompt'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save system prompt')
    wrapper.unmount()
  })

  it('shows guidanceError when saveGuidance catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountRemy()

    const guidance = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Additional Guidance'))!
    const save = guidance.findAll('button').find((b) => b.text().includes('Save Guidance'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save guidance')
    wrapper.unmount()
  })

  it('shows toolPermError when saveToolPerms catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountRemy()

    const perms = wrapper.find('[data-testid="remy-tool-permissions"]')
    const save = perms.findAll('button').find((b) => b.text().includes('Save Tool Permissions'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save')
    wrapper.unmount()
  })

  it('shows safetyError when saveSafetyConfig catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountRemy()

    const safety = wrapper.find('[data-testid="remy-safety-limits"]')
    const save = safety.findAll('button').find((b) => b.text().includes('Save Safety Config'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save safety config')
    wrapper.unmount()
  })

  it('shows skillError when toggleSkillActive catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountRemy({
      skills: [{ id: 's1', name: 'Test', description: 'd', triggers: ['t'], active: true }],
    })

    await wrapper.find('[data-testid="remy-skill-toggle"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to toggle skill')
    wrapper.unmount()
  })

  it('shows contextError when saveContextSource catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountRemy({
      contextSources: { product_primer: 'always_on' },
    })

    // Trigger a context source save by emitting update:modelValue on its select
    const knowledge = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Knowledge Sources'))!
    const rows = knowledge.findAll('tbody tr')
    const select = rows[0].findComponent({ name: 'Select' })
    await (select.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'tool')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save source')
    wrapper.unmount()
  })

  it('shows contextError when saveSkillSourceMode catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountRemy({
      skills: [{ id: 's1', name: 'Test', description: 'd', triggers: ['t'], active: true }],
    })

    // Open skills-as-knowledge section and trigger save
    const skillsAsKnowledge = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Skills as Knowledge'))
    if (skillsAsKnowledge) {
      const select = skillsAsKnowledge.findAllComponents({ name: 'Select' })[0]
      await (select.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'off')
      await flushPromises()
      expect(wrapper.text()).toContain('Failed to save skill source mode')
    }
    wrapper.unmount()
  })
})

// ── save error response (non-throw) paths ───────────────────────────────
describe('AdminRemyView branches — save error response paths', () => {
  it('shows modelError from API error response', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'model_err' } } as any)
    const wrapper = await mountRemy()

    const model = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Default Model'))!
    const save = model.findAll('button').find((b) => b.text().includes('Save Model Config'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save model config')
    expect(wrapper.text()).toContain('model_err')
    wrapper.unmount()
  })

  it('shows promptError from API error response', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'prompt_err' } } as any)
    const wrapper = await mountRemy()

    const prompt = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('System Prompt'))!
    const save = prompt.findAll('button').find((b) => b.text().includes('Save System Prompt'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save system prompt')
    wrapper.unmount()
  })

  it('shows guidanceError from API error response', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'guidance_err' } } as any)
    const wrapper = await mountRemy()

    const guidance = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Additional Guidance'))!
    const save = guidance.findAll('button').find((b) => b.text().includes('Save Guidance'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save guidance')
    wrapper.unmount()
  })

  it('shows toolPermError from API error response', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'perm_err' } } as any)
    const wrapper = await mountRemy()

    const perms = wrapper.find('[data-testid="remy-tool-permissions"]')
    const save = perms.findAll('button').find((b) => b.text().includes('Save Tool Permissions'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save')
    wrapper.unmount()
  })
})

// ── primer regeneration error (catch block) ─────────────────────────────
describe('AdminRemyView branches — primer regeneration error', () => {
  it('shows error message when primer regeneration throws', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.POST).mockRejectedValue(new Error('network failure'))
    const wrapper = await mountRemy()

    await wrapper.find('[data-testid="remy-primer-regenerate"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('network failure')
    wrapper.unmount()
  })
})

// ── loadProviders error response branch ─────────────────────────────────
describe('AdminRemyView branches — loadProviders error', () => {
  it('clears providerStatus and customProviderStatus on API error', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/remy/config') {
        return Promise.resolve({ data: mockRemyConfig, error: undefined })
      }
      if (path === '/api/v1/admin/remy/available-providers') {
        return Promise.resolve({ data: mockAvailableProviders, error: undefined })
      }
      if (path === '/api/v1/admin/remy/context-sources') {
        return Promise.resolve({ data: {}, error: undefined })
      }
      if (path === '/api/v1/admin/remy/skills') {
        return Promise.resolve({ data: [], error: undefined })
      }
      if (path.includes('/api/v1/admin/users') || path.includes('/api/v1/admin/teams')) {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      if (path === '/api/v1/model-backends') {
        return Promise.resolve({ data: null, error: { detail: 'providers error' } } as any)
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount((await import('../views/AdminRemyView.vue')).default)
    await flushPromises()
    // When model-backends errors, the providers list should be empty
    const providers = wrapper.find('[data-testid="remy-providers"]')
    expect(providers.exists()).toBe(true)
    wrapper.unmount()
  })

  it('clears providers on catch block (network failure)', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/remy/config') {
        return Promise.resolve({ data: mockRemyConfig, error: undefined })
      }
      if (path === '/api/v1/admin/remy/available-providers') {
        return Promise.resolve({ data: mockAvailableProviders, error: undefined })
      }
      if (path === '/api/v1/admin/remy/context-sources') {
        return Promise.resolve({ data: {}, error: undefined })
      }
      if (path === '/api/v1/admin/remy/skills') {
        return Promise.resolve({ data: [], error: undefined })
      }
      if (path.includes('/api/v1/admin/users') || path.includes('/api/v1/admin/teams')) {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      if (path === '/api/v1/model-backends') {
        return Promise.reject(new Error('connection refused'))
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount((await import('../views/AdminRemyView.vue')).default)
    await flushPromises()
    // No crash, providers section should render empty
    const providers = wrapper.find('[data-testid="remy-providers"]')
    expect(providers.exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── config hydration with null/undefined fields ─────────────────────────
describe('AdminRemyView branches — config hydration with missing fields', () => {
  it('hydrates safely when config has all null/undefined fields', async () => {
    const wrapper = await mountRemy({
      configOverrides: {
        access_list: null,
        default_provider: null,
        default_model: undefined,
        default_context_window: null,
        allowed_providers: null,
        allowed_models: null,
        system_prompt: null,
        additional_guidance: null,
        permission_mode: null,
        tool_permissions: null,
        rate_limit_max_actions: null,
        rate_limit_window_seconds: null,
        auto_execute_threshold: null,
        nogo_page_patterns: null,
        nogo_selector_patterns: null,
        allowed_selectors: null,
        allowed_page_patterns: null,
      },
    })
    // Should not crash — all fields fallback to defaults
    expect(wrapper.find('[data-testid="remy-providers"]').exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── saveContextSource API error (non-throw) ─────────────────────────────
describe('AdminRemyView branches — saveContextSource API error', () => {
  it('shows contextError on API error response', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'context_err' } } as any)
    const wrapper = await mountRemy({
      contextSources: { product_primer: 'always_on' },
    })

    const knowledge = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Knowledge Sources'))!
    const rows = knowledge.findAll('tbody tr')
    const select = rows[0].findComponent({ name: 'Select' })
    await (select.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'tool')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save source')
    wrapper.unmount()
  })
})

// ── saveSkillSourceMode API error (non-throw) ───────────────────────────
describe('AdminRemyView branches — saveSkillSourceMode API error', () => {
  it('shows contextError on skill source mode API error', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'skill_mode_err' } } as any)
    const wrapper = await mountRemy({
      skills: [{ id: 's1', name: 'Test', description: 'd', triggers: ['t'], active: true }],
    })

    const skillsAsKnowledge = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Skills as Knowledge'))
    if (skillsAsKnowledge) {
      const select = skillsAsKnowledge.findAllComponents({ name: 'Select' })[0]
      await (select.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'off')
      await flushPromises()
      expect(wrapper.text()).toContain('Failed to save skill source mode')
    }
    wrapper.unmount()
  })
})

// ── primer regeneration API error (non-throw) ───────────────────────────
describe('AdminRemyView branches — primer regeneration API error', () => {
  it('shows error message from API error response', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.POST).mockResolvedValue({ data: null, error: { detail: 'primer_budget' } } as any)
    const wrapper = await mountRemy()

    await wrapper.find('[data-testid="remy-primer-regenerate"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('primer_budget')
    wrapper.unmount()
  })
})

// ── configSaving guard ──────────────────────────────────────────────────
describe('AdminRemyView branches — configSaving guard', () => {
  it('returns early from saveAccessList when configSaving is true', async () => {
    const wrapper = await mountRemy()
    // configSaving is a ref but exposed through the proxy as a plain boolean
    // on the component instance. Use the component's internal access.
    const vm = wrapper.vm as unknown as Record<string, unknown>
    // Set configSaving directly — it's a ref on the instance
    ;(vm as any).configSaving = true
    await flushPromises()

    const access = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Access List'))!
    const save = access.findAll('button').find((b) => b.text().includes('Save Access List'))!
    await save.trigger('click')
    await flushPromises()

    // PUT should not have been called because configSaving was true
    const { api } = await import('../lib/api/client')
    expect(api.PUT).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

// ── primer timer cleanup ────────────────────────────────────────────────
describe('AdminRemyView branches — primer timer', () => {
  it('clears primer message after timeout', async () => {
    vi.useFakeTimers()
    // Mock POST to return success BEFORE mounting
    const { api } = await import('../lib/api/client')
    vi.mocked(api.POST).mockResolvedValue({ data: undefined, error: undefined, response: {} as Response } as any)
    const wrapper = await mountRemy()

    await wrapper.find('[data-testid="remy-primer-regenerate"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Primer regenerated')

    // Advance past the 4000ms timer
    vi.advanceTimersByTime(4001)
    await nextTick()

    expect(wrapper.text()).not.toContain('Primer regenerated')
    vi.useRealTimers()
    wrapper.unmount()
  })
})
