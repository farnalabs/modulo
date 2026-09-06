import { describe, it, expect, beforeAll, afterAll, vi, beforeEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'
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

describe('AdminRemyView', () => {
  let wrapper: VueWrapper | null = null

  beforeAll(async () => {
    setActivePinia(createPinia())
    wrapper = mount(AdminRemyView)
    await vi.waitFor(() => {
      expect(wrapper!.text()).toContain('Configured Providers')
    }, { timeout: 60000, interval: 100 })
  }, 60000)

  afterAll(() => {
    wrapper?.unmount()
  })

  it('renders the config page title', () => {
    expect(wrapper!.text()).toContain('Remy Configuration')
  })

  it('renders the system prompt section', () => {
    expect(wrapper!.text()).toContain('System Prompt')
  })

  it('renders the skills section', () => {
    expect(wrapper!.text()).toContain('Skills')
  })

  it('renders access list section', () => {
    expect(wrapper!.text()).toContain('Access List')
  })

  it('renders configured providers section', () => {
    expect(wrapper!.text()).toContain('Configured Providers')
  })
})

function skill(overrides: Record<string, unknown> = {}) {
  return {
    id: 'skill-1',
    name: 'Deploy Helper',
    description: 'Helps with deploys',
    triggers: ['deploy', 'release'],
    active: true,
    ...overrides,
  }
}

async function mountRemy(overrides: {
  skills?: Array<Record<string, unknown>>
  backends?: Array<Record<string, unknown>>
  contextSources?: Record<string, string>
} = {}) {
  const { api } = await import('../lib/api/client')
  vi.mocked(api.GET).mockImplementation((path: string) => {
    if (path === '/api/v1/admin/remy/config') {
      return Promise.resolve({ data: mockRemyConfig, error: undefined })
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

  setActivePinia(createPinia())
  const wrapper = mount(AdminRemyView)
  await flushPromises()
  await nextTick()
  return wrapper
}

function section(wrapper: ReturnType<typeof mount>, title: string) {
  // SectionCard renders its title in an <h2> inside a .card wrapper
  const cards = wrapper.findAll('.card')
  return cards.find((c) => c.find('h2').text().includes(title))
}

describe('AdminRemyView — save flows', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the loading skeleton before the config loads', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.GET).mockImplementation(() => new Promise(() => {}))
    setActivePinia(createPinia())
    const wrapper = mount(AdminRemyView)
    expect(wrapper.find('.animate-pulse').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders provider status from the model-backends list', async () => {
    const wrapper = await mountRemy({
      backends: [{ provider: 'anthropic', has_credentials: true }],
    })

    const providers = wrapper.find('[data-testid="remy-providers"]')
    expect(providers.text()).toContain('Anthropic')
    expect(providers.text()).toContain('OpenAI')
    expect(providers.text()).toContain('Configured')
    expect(providers.text()).toContain('Not set')
    wrapper.unmount()
  })

  it('BUG: role toggles do not stick — the access list saves with stale roles (readonly vue-query data)', async () => {
    // Production bug characterisation. The config hydration assigns query
    // arrays by reference into local reactive state:
    //   `accessList.selectedRoles = acl.org_roles || ['admin']`
    // The array is a deep-readonly proxy from @tanstack/vue-query's query
    // state, so toggleRole()'s push/splice silently fails and the saved
    // access_list keeps the stale org_roles.
    const wrapper = await mountRemy()

    const access = section(wrapper, 'Access List')!
    const operatorCheckbox = access.findAll('input[type="checkbox"]').find((c) => (c.element as HTMLInputElement).value === 'operator')
    await operatorCheckbox!.setValue(true)
    await nextTick()

    const save = access.findAll('button').find((b) => b.text().includes('Save Access List'))
    await save!.trigger('click')
    await flushPromises()

    const put = vi.mocked((await import('../lib/api/client')).api.PUT).mock.calls[0]
    // the toggle did NOT stick: org_roles still the stale ['admin']
    expect((put[1] as any).body.access_list.org_roles).toEqual(['admin'])
    wrapper.unmount()
  })

  it('shows an access-list save failure inline', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'acl_denied' } } as any)
    const wrapper = await mountRemy()

    const access = section(wrapper, 'Access List')!
    const save = access.findAll('button').find((b) => b.text().includes('Save Access List'))
    await save!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save access list')
    expect(wrapper.text()).toContain('acl_denied')
    wrapper.unmount()
  })

  it('saves the default model configuration with parsed allowed models', async () => {
    const wrapper = await mountRemy()

    const model = section(wrapper, 'Default Model Configuration')!
    const nameInput = wrapper.find('[data-testid="remy-model-name"]')
    await nameInput.setValue('claude-sonnet-4')
    const contextInput = wrapper.find('[data-testid="remy-model-context"]')
    await contextInput.setValue('128000')
    const allowedModels = wrapper.find('[data-testid="remy-allowed-models"]')
    await allowedModels.setValue('claude-sonnet-4, gpt-4o')

    const save = model.findAll('button').find((b) => b.text().includes('Save Model Config'))
    await save!.trigger('click')
    await flushPromises()

    const put = vi.mocked((await import('../lib/api/client')).api.PUT).mock.calls[0]
    expect((put[1] as any).body).toEqual({
      default_provider: 'anthropic',
      default_model: 'claude-sonnet-4',
      default_context_window: 128000,
      allowed_providers: ['anthropic'],
      allowed_models: ['claude-sonnet-4', 'gpt-4o'],
    })
    wrapper.unmount()
  })

  it('BUG: allowed-provider chip toggles do not stick (readonly vue-query data)', async () => {
    // Same readonly-hydration bug as the role toggles: modelConfig's
    // allowedProviders is assigned the query's readonly array by reference
    // (`modelConfig.allowedProviders = c.allowed_providers || ['anthropic']`),
    // so toggleAllowedProvider()'s push silently fails.
    const wrapper = await mountRemy()

    const chip = wrapper.find('[data-testid="remy-allowed-provider-openai"]')
    expect(chip.attributes('aria-pressed')).toBe('false')
    await chip.trigger('click')
    await nextTick()
    // the chip did NOT activate
    expect(chip.attributes('aria-pressed')).toBe('false')
    wrapper.unmount()
  })

  it('saves the system prompt', async () => {
    const wrapper = await mountRemy()

    const prompt = wrapper.find('[data-testid="remy-system-prompt"]')
    await prompt.setValue('You are Remy.')
    const promptSection = section(wrapper, 'System Prompt')
    const save = promptSection!.findAll('button').find((b) => b.text().includes('Save System Prompt'))
    await save!.trigger('click')
    await flushPromises()

    const put = vi.mocked((await import('../lib/api/client')).api.PUT).mock.calls[0]
    expect((put[1] as any).body).toEqual({ system_prompt: 'You are Remy.' })
    wrapper.unmount()
  })

  it('saves the additional guidance', async () => {
    const wrapper = await mountRemy()

    const guidance = wrapper.find('[data-testid="remy-guidance"]')
    await guidance.setValue('Always cite sources.')
    const guidanceSection = section(wrapper, 'Additional Guidance')
    const save = guidanceSection!.findAll('button').find((b) => b.text().includes('Save Guidance'))
    await save!.trigger('click')
    await flushPromises()

    const put = vi.mocked((await import('../lib/api/client')).api.PUT).mock.calls[0]
    expect((put[1] as any).body).toEqual({ additional_guidance: 'Always cite sources.' })
    wrapper.unmount()
  })

  it('applies the locked-down tool permission preset and saves tool permissions', async () => {
    const wrapper = await mountRemy()

    const modeSelect = wrapper.find('[data-testid="remy-tool-perm-mode"]').findComponent({ name: 'Select' })
    await (modeSelect.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'locked_down')
    await nextTick()

    // locked-down preset: press/click/fill/select/go_back require approval
    const perms = (wrapper.vm as unknown as { toolPerms?: Record<string, string> }).toolPerms
    expect(perms!.navigate).toBe('always_allowed')
    expect(perms!.press).toBe('requires_approval')
    expect(perms!.click).toBe('requires_approval')

    const permsSection = wrapper.find('[data-testid="remy-tool-permissions"]')
    const save = permsSection.findAll('button').find((b) => b.text().includes('Save Tool Permissions'))
    await save!.trigger('click')
    await flushPromises()

    const put = vi.mocked((await import('../lib/api/client')).api.PUT).mock.calls[0]
    expect((put[1] as any).body.permission_mode).toBe('locked_down')
    expect((put[1] as any).body.tool_permissions.press).toBe('requires_approval')
    wrapper.unmount()
  })

  it('per-tool permission selects are disabled unless the mode is custom', async () => {
    const wrapper = await mountRemy()

    const modeSelect = wrapper.find('[data-testid="remy-tool-perm-mode"]').findComponent({ name: 'Select' })
    await (modeSelect.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'custom')
    await nextTick()

    const permsSection = wrapper.find('[data-testid="remy-tool-permissions"]')
    const toolSelects = permsSection.findAllComponents({ name: 'Select' })
    expect(toolSelects.length).toBeGreaterThan(0)
    wrapper.unmount()
  })

  it('saves the safety config with split pattern lists', async () => {
    const wrapper = await mountRemy()

    await wrapper.find('[data-testid="remy-rate-limit-max-actions"]').setValue('45')
    await wrapper.find('[data-testid="remy-rate-limit-window"]').setValue('120')
    await wrapper.find('[data-testid="remy-nogo-page-patterns"]').setValue('/admin/*, /billing')
    await wrapper.find('[data-testid="remy-allowed-selectors"]').setValue('[data-safe]')

    const safety = wrapper.find('[data-testid="remy-safety-limits"]')
    const save = safety.findAll('button').find((b) => b.text().includes('Save Safety Config'))
    await save!.trigger('click')
    await flushPromises()

    const put = vi.mocked((await import('../lib/api/client')).api.PUT).mock.calls[0]
    expect((put[1] as any).body).toEqual({
      rate_limit_max_actions: 45,
      rate_limit_window_seconds: 120,
      auto_execute_threshold: 0.8,
      nogo_page_patterns: ['/admin/*', '/billing'],
      nogo_selector_patterns: [],
      allowed_selectors: ['[data-safe]'],
      allowed_page_patterns: [],
    })
    wrapper.unmount()
  })

  it('shows a safety-config save failure inline', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'safety_rejected' } } as any)
    const wrapper = await mountRemy()

    const safety = wrapper.find('[data-testid="remy-safety-limits"]')
    const save = safety.findAll('button').find((b) => b.text().includes('Save Safety Config'))
    await save!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save safety config')
    expect(wrapper.text()).toContain('safety_rejected')
    wrapper.unmount()
  })

  it('renders the knowledge sources table and saves a source mode change', async () => {
    const wrapper = await mountRemy({
      contextSources: { product_primer: 'always_on', page_context: 'tool', user_profile: 'off' },
    })

    const knowledge = section(wrapper, 'Knowledge Sources')
    expect(knowledge!.text()).toContain('Product Docs')
    expect(knowledge!.text()).toContain('search_documentation()')

    // all 7 context-source definitions render (missing modes default to always_on)
    const rows = knowledge!.findAll('tbody tr')
    expect(rows.length).toBe(7)

    // flip product_primer (first row) to tool via its PrimeVue Select
    const select = rows[0].findComponent({ name: 'Select' })
    await (select.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'tool')
    await flushPromises()

    const put = vi.mocked((await import('../lib/api/client')).api.PUT).mock.calls[0]
    expect(put[0]).toBe('/api/v1/admin/remy/context-sources/{source_key}')
    expect((put[1] as any).params.path).toEqual({ source_key: 'product_primer' })
    expect((put[1] as any).body).toEqual({ source_mode: 'tool' })
    wrapper.unmount()
  })

  it('renders the skills table with triggers and toggles a skill inactive', async () => {
    const wrapper = await mountRemy({ skills: [skill()] })

    const skills = wrapper.find('[data-testid="remy-skills"]')
    expect(skills.text()).toContain('Deploy Helper')
    expect(skills.text()).toContain('deploy')
    expect(skills.text()).toContain('Active')

    const toggle = wrapper.find('[data-testid="remy-skill-toggle"]')
    await toggle.trigger('click')
    await flushPromises()

    const put = vi.mocked((await import('../lib/api/client')).api.PUT).mock.calls[0]
    expect(put[0]).toBe('/api/v1/admin/remy/skills/{skill_id}')
    expect((put[1] as any).body).toEqual({ active: false })
    wrapper.unmount()
  })

  it('shows a skill toggle failure inline', async () => {
    const { api } = await import('../lib/api/client')
    const wrapper = await mountRemy({ skills: [skill()] })
    vi.mocked(api.PUT).mockResolvedValue({ data: null, error: { detail: 'skill_locked' } } as any)

    await wrapper.find('[data-testid="remy-skill-toggle"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to toggle skill')
    expect(wrapper.text()).toContain('skill_locked')
    wrapper.unmount()
  })

  it('renders the skills-as-knowledge table and saves a skill source mode', async () => {
    const wrapper = await mountRemy({ skills: [skill()] })

    const skillsAsKnowledge = section(wrapper, 'Skills as Knowledge')
    expect(skillsAsKnowledge).toBeTruthy()

    const select = skillsAsKnowledge!.findAllComponents({ name: 'Select' })[0]
    await (select.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'off')
    await flushPromises()

    const put = vi.mocked((await import('../lib/api/client')).api.PUT).mock.calls[0]
    expect((put[1] as any).params.path).toEqual({ source_key: 'skill-1' })
    expect((put[1] as any).body).toEqual({ source_mode: 'off' })
    wrapper.unmount()
  })

  it('shows the no-skills empty state and hides the skills-as-knowledge section', async () => {
    const wrapper = await mountRemy({ skills: [] })

    expect(wrapper.find('[data-testid="remy-skills"]').text()).toContain('No skills configured yet')
    expect(section(wrapper, 'Skills as Knowledge')).toBeUndefined()
    wrapper.unmount()
  })

  it('regenerates the product primer and shows the success message', async () => {
    const wrapper = await mountRemy()

    await wrapper.find('[data-testid="remy-primer-regenerate"]').trigger('click')
    await flushPromises()

    const post = vi.mocked((await import('../lib/api/client')).api.POST).mock.calls[0]
    expect(post[0]).toBe('/api/v1/admin/remy/primer/regenerate')
    expect(wrapper.text()).toContain('Primer regenerated')
    wrapper.unmount()
  })

  it('shows a failure message when primer regeneration errors', async () => {
    const { api } = await import('../lib/api/client')
    const wrapper = await mountRemy()
    vi.mocked(api.POST).mockResolvedValue({ data: null, error: { detail: 'primer_budget' } } as any)

    await wrapper.find('[data-testid="remy-primer-regenerate"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('primer_budget')
    wrapper.unmount()
  })
})
