/**
 * Branch coverage tests for AdminAssistantView.vue (FAR-835).
 *
 * Targets uncovered branches: provider tooltip variants, custom providers
 * empty state, skills with empty triggers/description, context source
 * token vs toolCall display, save error catch blocks, loadProviders error
 * paths, the removed primer-regenerate control (FAR-1724 absence pins),
 * config hydration null/undefined, and
 * all error-state display branches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../components/FeatureGate.vue', () => ({
  default: { template: '<div><slot /></div>' },
}))

const mockAssistantConfig = {
  access_list: { user_ids: [], team_ids: [], org_roles: ['admin'] },
  default_provider: 'anthropic',
  default_model: '',
  default_context_window: 200000,
  allowed_providers: ['anthropic'],
  allowed_models: [],
  system_prompt: '',
  additional_guidance: '',
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
      if (path === '/api/v1/admin/assistant/config') {
        return Promise.resolve({ data: mockAssistantConfig, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/available-providers') {
        return Promise.resolve({ data: mockAvailableProviders, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/context-sources') {
        return Promise.resolve({ data: { product_primer: 'always_on', page_context: 'always_on', user_profile: 'always_on' }, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/skills') {
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

async function mountAssistant(overrides: {
  skills?: Array<Record<string, unknown>>
  backends?: Array<Record<string, unknown>>
  contextSources?: Record<string, string>
  configOverrides?: Record<string, unknown>
} = {}) {
  const { api } = await import('../lib/api/client')
  vi.mocked(api.GET).mockImplementation((path: string) => {
    if (path === '/api/v1/admin/assistant/config') {
      return Promise.resolve({ data: { ...mockAssistantConfig, ...overrides.configOverrides }, error: undefined })
    }
    if (path === '/api/v1/admin/assistant/available-providers') {
      return Promise.resolve({ data: mockAvailableProviders, error: undefined })
    }
    if (path === '/api/v1/admin/assistant/context-sources') {
      return Promise.resolve({ data: overrides.contextSources ?? {}, error: undefined })
    }
    if (path === '/api/v1/admin/assistant/skills') {
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

  const wrapper = mount((await import('../views/AdminAssistantView.vue')).default)
  await flushPromises()
  await nextTick()
  return wrapper
}

// ── providerTooltip branches ─────────────────────────────────────────────
describe('AdminAssistantView branches — providerTooltip', () => {
  it('configured provider tooltip contains "api key configured"', async () => {
    const wrapper = await mountAssistant({
      backends: [{ provider: 'anthropic', has_credentials: true }],
    })
    const providers = wrapper.find('[data-testid="assistant-providers"]')
    // Configured provider should show the configured styling (not muted)
    expect(providers.text()).toContain('Anthropic')
    expect(providers.text()).toContain('Configured')
    wrapper.unmount()
  })

  it('unconfigured provider tooltip contains "no api key set"', async () => {
    const wrapper = await mountAssistant({
      backends: [],
    })
    const providers = wrapper.find('[data-testid="assistant-providers"]')
    expect(providers.text()).toContain('Not set')
    wrapper.unmount()
  })
})

// ── customProviderStatus empty state ────────────────────────────────────
describe('AdminAssistantView branches — custom providers', () => {
  it('shows empty message when customProviderStatus is empty', async () => {
    // Override available-providers to have no customTypes
    const { api } = await import('../lib/api/client')
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/assistant/config') {
        return Promise.resolve({ data: mockAssistantConfig, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/available-providers') {
        return Promise.resolve({ data: { native: mockAvailableProviders.native, customTypes: [] }, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/context-sources') {
        return Promise.resolve({ data: {}, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/skills') {
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
    const wrapper = mount((await import('../views/AdminAssistantView.vue')).default)
    await flushPromises()
    const customSection = wrapper.find('[data-testid="assistant-custom-backends"]')
    expect(customSection.text()).toContain('No custom backends configured')
    wrapper.unmount()
  })

  it('shows custom providers when available', async () => {
    const wrapper = await mountAssistant({
      backends: [{ provider: 'custom-1', has_credentials: true }],
    })
    const customSection = wrapper.find('[data-testid="assistant-custom-backends"]')
    expect(customSection.text()).toContain('Custom Provider')
    wrapper.unmount()
  })
})

// ── providersLoading in custom backends section ─────────────────────────
describe('AdminAssistantView branches — providersLoading', () => {
  it('shows loading in the configured providers section while providers load', async () => {
    const { api } = await import('../lib/api/client')
    // Mock all endpoints except model-backends (which provides provider data)
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/assistant/config') {
        return Promise.resolve({ data: mockAssistantConfig, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/available-providers') {
        return Promise.resolve({ data: mockAvailableProviders, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/context-sources') {
        return Promise.resolve({ data: {}, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/skills') {
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
    const wrapper = mount((await import('../views/AdminAssistantView.vue')).default)
    await flushPromises()
    // When providersLoading is true, the main loading computed is true,
    // so the loading skeleton renders (the providers section is not yet visible)
    expect(wrapper.findAll('.animate-pulse').length).toBeGreaterThan(0)
    wrapper.unmount()
  })
})

// ── skills with empty triggers and empty description ────────────────────
describe('AdminAssistantView branches — skills rendering', () => {
  it('shows dash for empty triggers array', async () => {
    const wrapper = await mountAssistant({
      skills: [{ id: 's1', name: 'Test Skill', description: 'desc', triggers: [], active: true }],
    })
    const skills = wrapper.find('[data-testid="assistant-skills"]')
    expect(skills.text()).toContain('—')
    wrapper.unmount()
  })

  it('shows dash for null triggers', async () => {
    const wrapper = await mountAssistant({
      skills: [{ id: 's1', name: 'Test Skill', description: 'desc', triggers: null, active: true }],
    })
    const skills = wrapper.find('[data-testid="assistant-skills"]')
    expect(skills.text()).toContain('—')
    wrapper.unmount()
  })

  it('shows dash for empty description', async () => {
    const wrapper = await mountAssistant({
      skills: [{ id: 's1', name: 'Test Skill', description: '', triggers: [], active: true }],
    })
    const skills = wrapper.find('[data-testid="assistant-skills"]')
    expect(skills.text()).toContain('—')
    wrapper.unmount()
  })

  it('shows dash for null description', async () => {
    const wrapper = await mountAssistant({
      skills: [{ id: 's1', name: 'Test Skill', description: null, triggers: [], active: true }],
    })
    const skills = wrapper.find('[data-testid="assistant-skills"]')
    expect(skills.text()).toContain('—')
    wrapper.unmount()
  })
})

// ── context source: tokens vs toolCall display ──────────────────────────
describe('AdminAssistantView branches — context source display', () => {
  it('shows token count for sources with tokens', async () => {
    const wrapper = await mountAssistant({
      contextSources: { product_primer: 'always_on', page_context: 'tool' },
    })
    // product_primer has tokens: '~700'
    expect(wrapper.text()).toContain('~700')
    wrapper.unmount()
  })

  it('shows toolCall for sources with toolCall', async () => {
    const wrapper = await mountAssistant({
      contextSources: { product_primer: 'always_on' },
    })
    // product_docs has toolCall: 'search_documentation()'
    expect(wrapper.text()).toContain('search_documentation()')
    wrapper.unmount()
  })
})

// ── save error catch blocks ─────────────────────────────────────────────
describe('AdminAssistantView branches — save error catch blocks', () => {
  it('shows accessError when saveAccessList catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountAssistant()

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
    const wrapper = await mountAssistant()

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
    const wrapper = await mountAssistant()

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
    const wrapper = await mountAssistant()

    const guidance = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Additional Guidance'))!
    const save = guidance.findAll('button').find((b) => b.text().includes('Save Guidance'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save guidance')
    wrapper.unmount()
  })

  it('shows skillError when toggleSkillActive catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountAssistant({
      skills: [{ id: 's1', name: 'Test', description: 'd', triggers: ['t'], active: true }],
    })

    await wrapper.find('[data-testid="assistant-skill-toggle"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to toggle skill')
    wrapper.unmount()
  })

  it('shows contextError when saveContextSource catches', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockRejectedValue(new Error('network'))
    const wrapper = await mountAssistant({
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
    const wrapper = await mountAssistant({
      skills: [{ id: 's1', name: 'Test', description: 'd', triggers: ['t'], active: true }],
    })

    // Open skills-as-knowledge section and trigger save
    const skillsAsKnowledge = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Skills as Knowledge'))
    expect(skillsAsKnowledge).toBeDefined()
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
describe('AdminAssistantView branches — save error response paths', () => {
  it('shows modelError from API error response', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'model_err' } } as any)
    const wrapper = await mountAssistant()

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
    const wrapper = await mountAssistant()

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
    const wrapper = await mountAssistant()

    const guidance = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Additional Guidance'))!
    const save = guidance.findAll('button').find((b) => b.text().includes('Save Guidance'))!
    await save.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save guidance')
    wrapper.unmount()
  })
})

// ── dead primer-regenerate control removal (FAR-1724) ───────────────────
// POST /api/v1/admin/assistant/primer/regenerate never existed on the
// backend; the control was removed. These pin the absence and that no
// POST to the dead route is ever issued.
describe('AdminAssistantView branches — primer-regenerate control removed', () => {
  it('does not render the primer-regenerate button', async () => {
    const wrapper = await mountAssistant()

    expect(wrapper.find('[data-testid="assistant-primer-regenerate"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Regenerate Primer')
    wrapper.unmount()
  })

  it('never POSTs to the dead primer/regenerate route', async () => {
    const { api } = await import('../lib/api/client')
    const wrapper = await mountAssistant()
    await flushPromises()

    const posts = vi.mocked(api.POST).mock.calls.map((c) => c[0])
    expect(posts).not.toContain('/api/v1/admin/assistant/primer/regenerate')
    wrapper.unmount()
  })
})

// ── loadProviders error response branch ─────────────────────────────────
describe('AdminAssistantView branches — loadProviders error', () => {
  it('clears providerStatus and customProviderStatus on API error', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/assistant/config') {
        return Promise.resolve({ data: mockAssistantConfig, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/available-providers') {
        return Promise.resolve({ data: mockAvailableProviders, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/context-sources') {
        return Promise.resolve({ data: {}, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/skills') {
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
    const wrapper = mount((await import('../views/AdminAssistantView.vue')).default)
    await flushPromises()
    // When model-backends errors, providerStatus is cleared, so the
    // configured-providers section renders no provider entries...
    const providers = wrapper.find('[data-testid="assistant-providers"]')
    expect(providers.exists()).toBe(true)
    expect(providers.text()).not.toContain('Anthropic')
    // ...and the custom-backends section falls back to its empty state.
    const customSection = wrapper.find('[data-testid="assistant-custom-backends"]')
    expect(customSection.text()).toContain('No custom backends configured')
    wrapper.unmount()
  })

  it('clears providers on catch block (network failure)', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/assistant/config') {
        return Promise.resolve({ data: mockAssistantConfig, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/available-providers') {
        return Promise.resolve({ data: mockAvailableProviders, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/context-sources') {
        return Promise.resolve({ data: {}, error: undefined })
      }
      if (path === '/api/v1/admin/assistant/skills') {
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
    const wrapper = mount((await import('../views/AdminAssistantView.vue')).default)
    await flushPromises()
    // No crash, providerStatus is cleared on the catch path, so the
    // configured-providers section renders no entries and the
    // custom-backends section shows its empty state.
    const providers = wrapper.find('[data-testid="assistant-providers"]')
    expect(providers.exists()).toBe(true)
    expect(providers.text()).not.toContain('Anthropic')
    const customSection = wrapper.find('[data-testid="assistant-custom-backends"]')
    expect(customSection.text()).toContain('No custom backends configured')
    wrapper.unmount()
  })
})

// ── config hydration with null/undefined fields ─────────────────────────
describe('AdminAssistantView branches — config hydration with missing fields', () => {
  it('hydrates safely when config has all null/undefined fields', async () => {
    const wrapper = await mountAssistant({
      configOverrides: {
        access_list: null,
        default_provider: null,
        default_model: undefined,
        default_context_window: null,
        allowed_providers: null,
        allowed_models: null,
        system_prompt: null,
        additional_guidance: null,
      },
    })
    // Should not crash — all fields fallback to defaults
    expect(wrapper.find('[data-testid="assistant-providers"]').exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── saveContextSource API error (non-throw) ─────────────────────────────
describe('AdminAssistantView branches — saveContextSource API error', () => {
  it('shows contextError on API error response', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'context_err' } } as any)
    const wrapper = await mountAssistant({
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
describe('AdminAssistantView branches — saveSkillSourceMode API error', () => {
  it('shows contextError on skill source mode API error', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'skill_mode_err' } } as any)
    const wrapper = await mountAssistant({
      skills: [{ id: 's1', name: 'Test', description: 'd', triggers: ['t'], active: true }],
    })

    const skillsAsKnowledge = wrapper.findAll('.card').find((c) => c.find('h2')?.text().includes('Skills as Knowledge'))
    expect(skillsAsKnowledge).toBeDefined()
    if (skillsAsKnowledge) {
      const select = skillsAsKnowledge.findAllComponents({ name: 'Select' })[0]
      await (select.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'off')
      await flushPromises()
      expect(wrapper.text()).toContain('Failed to save skill source mode')
    }
    wrapper.unmount()
  })
})

// ── configSaving guard ──────────────────────────────────────────────────
describe('AdminAssistantView branches — configSaving guard', () => {
  it('returns early from saveAccessList when configSaving is true', async () => {
    const wrapper = await mountAssistant()
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

// ── inactive skill toggle class branch ──────────────────────────────────
describe('AdminAssistantView branches — inactive skill toggle', () => {
  it('renders an inactive skill toggle with the muted token class', async () => {
    const wrapper = await mountAssistant({
      skills: [{ id: 's1', name: 'Inactive Skill', description: 'd', triggers: ['t'], active: false }],
    })

    const toggle = wrapper.find('[data-testid="assistant-skill-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.classes()).toContain('bg-muted')
    expect(toggle.classes()).toContain('text-muted-foreground')
    wrapper.unmount()
  })
})
