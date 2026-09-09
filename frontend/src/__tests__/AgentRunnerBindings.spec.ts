import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() {
  await vueNextTick()
  await flushPromises()
}

const { mockGet, mockPut } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPut: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    PUT: mockPut,
  },
}))

import AgentRunnerBindings from '../components/agent/AgentRunnerBindings.vue'

interface BindingRow {
  model_backend_id: string
  target_env_var: string
  source_field: string
}

let backends: Array<{ id: string; name: string; provider: string }> = []
let bindingsByAgent: Record<string, BindingRow[]> = {}

function backend(id: string, name: string) {
  return { id, name, provider: 'anthropic' }
}

function row(targetEnvVar: string, backendId: string): BindingRow {
  return { model_backend_id: backendId, target_env_var: targetEnvVar, source_field: 'api_key' }
}

function mountBindings(agentId: string | null) {
  return mount(AgentRunnerBindings, { props: { agentId } })
}

async function selectBackend(wrapper: Awaited<ReturnType<typeof mountBindings>>, id: string) {
  const select = wrapper
    .find('[data-testid="pipeline-editor-runner-binding-backend"]')
    .findComponent({ name: 'Select' })
  await (select.vm as unknown as { $emit: (event: string, value: unknown) => void }).$emit(
    'update:modelValue',
    id,
  )
  await nextTick()
}

describe('AgentRunnerBindings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    backends = [backend('backend-1', 'Anthropic'), backend('backend-2', 'OpenAI')]
    bindingsByAgent = {}
    mockGet.mockImplementation((path: unknown, init?: unknown) => {
      if (path === '/api/v1/model-backends') {
        return Promise.resolve({ data: { items: backends }, error: undefined })
      }
      if (path === '/api/v1/agents/{agent_id}/bindings') {
        const agentId =
          (init as { params?: { path?: { agent_id?: string } } } | undefined)?.params?.path?.agent_id ?? ''
        return Promise.resolve({ data: { items: bindingsByAgent[agentId] ?? [] }, error: undefined })
      }
      return Promise.resolve({ data: undefined, error: undefined })
    })
    mockPut.mockResolvedValue({ data: { items: [] }, error: undefined })
  })

  it('loads model backends + bindings on mount for the given agent', async () => {
    bindingsByAgent = { 'agent-1': [row('API_KEY', 'backend-1')] }
    const wrapper = mountBindings('agent-1')
    await nextTick()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/model-backends')
    expect(mockGet).toHaveBeenCalledWith('/api/v1/agents/{agent_id}/bindings', {
      params: { path: { agent_id: 'agent-1' } },
    })
    expect(wrapper.text()).toContain('API_KEY')
  })

  it('does not load when agentId is absent', async () => {
    const wrapper = mountBindings(null)
    await nextTick()

    expect(mockGet).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="pipeline-editor-runner-bindings"]').exists()).toBe(false)
  })

  it('resetting on agentId switch: scraps the previous agent rows, inputs and reloads (cross-wiring regression)', async () => {
    bindingsByAgent = {
      'agent-a': [row('ALPHA_VAR', 'backend-1')],
      'agent-b': [row('BETA_VAR', 'backend-2')],
    }
    const wrapper = mountBindings('agent-a')
    await nextTick()
    expect(wrapper.text()).toContain('ALPHA_VAR')

    // Dirty the in-memory add-row state the way a half-finished edit does.
    await selectBackend(wrapper, 'backend-1')
    await wrapper
      .find('[data-testid="pipeline-editor-runner-binding-target-input"]')
      .setValue('STALE_DIRTY')
    await nextTick()

    await wrapper.setProps({ agentId: 'agent-b' })
    await nextTick()

    // The outgoing agent's rows and inputs are scrapped BEFORE the reload.
    expect(wrapper.text()).not.toContain('ALPHA_VAR')
    expect(wrapper.text()).toContain('BETA_VAR')
    expect(
      (wrapper.find('[data-testid="pipeline-editor-runner-binding-target-input"]').element as HTMLInputElement)
        .value,
    ).toBe('')
    // The reload targets the NEW agent.
    const bindingsCalls = mockGet.mock.calls.filter((call) => call[0] === '/api/v1/agents/{agent_id}/bindings')
    const lastCallInit = bindingsCalls[bindingsCalls.length - 1]?.[1] as
      | { params?: { path?: { agent_id?: string } } }
      | undefined
    expect(lastCallInit?.params?.path?.agent_id).toBe('agent-b')

    // And a save right after the switch sends ONLY the new agent's rows —
    // the previous agent's row must never be cross-wired into its save.
    await wrapper.find('[data-testid="pipeline-editor-runner-binding-save"]').trigger('click')
    await nextTick()
    const putCall = mockPut.mock.calls[0]
    expect(putCall[0]).toBe('/api/v1/agents/{agent_id}/bindings')
    const putInit = putCall[1] as { params: { path: { agent_id: string } }; body: { bindings: BindingRow[] } }
    expect(putInit.params.path.agent_id).toBe('agent-b')
    expect(putInit.body.bindings).toEqual([row('BETA_VAR', 'backend-2')])
  })

  it('switching to a null agentId clears the rows without a reload', async () => {
    bindingsByAgent = { 'agent-a': [row('ALPHA_VAR', 'backend-1')] }
    const wrapper = mountBindings('agent-a')
    await nextTick()
    expect(wrapper.text()).toContain('ALPHA_VAR')

    await wrapper.setProps({ agentId: null })
    await nextTick()

    expect(wrapper.text()).not.toContain('ALPHA_VAR')
    const bindingsCalls = mockGet.mock.calls.filter((call) => call[0] === '/api/v1/agents/{agent_id}/bindings')
    expect(bindingsCalls).toHaveLength(1) // only the agent-a mount load
  })

  it('blocks a case-insensitive duplicate target_env_var client-side with the duplicate banner', async () => {
    bindingsByAgent = { 'agent-a': [row('API_KEY', 'backend-1')] }
    const wrapper = mountBindings('agent-a')
    await nextTick()
    const rows = () => wrapper.findAll('[data-testid="pipeline-editor-runner-bindings-rows"] li')
    expect(rows()).toHaveLength(1)

    // The server canonicalises target_env_var to UPPERCASE — the lowercase
    // variant is a duplicate and must be caught BEFORE the confusing 409.
    await selectBackend(wrapper, 'backend-2')
    await wrapper
      .find('[data-testid="pipeline-editor-runner-binding-target-input"]')
      .setValue('api_key')
    await wrapper.find('[data-testid="pipeline-editor-runner-binding-add"]').trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain("The target env var 'API_KEY' is already bound on this agent.")
    expect(rows()).toHaveLength(1) // nothing added
    expect(mockPut).not.toHaveBeenCalled()

    // A genuinely-new var still adds a row. (Note: the duplicate banner is
    // NOT auto-cleared by a subsequent successful add — addRow only sets
    // error on rejection; save() clears it. Left as-is in FAR-595.)
    await wrapper
      .find('[data-testid="pipeline-editor-runner-binding-target-input"]')
      .setValue('OTHER_VAR')
    await wrapper.find('[data-testid="pipeline-editor-runner-binding-add"]').trigger('click')
    await nextTick()
    expect(rows()).toHaveLength(2)
  })

  it('surfaces the server error detail in the save-failure banner', async () => {
    bindingsByAgent = { 'agent-a': [row('API_KEY', 'backend-1')] }
    mockPut.mockResolvedValue({
      data: null,
      error: { detail: 'target_env_var MODULO_RESERVED is not allowed' },
    })
    const wrapper = mountBindings('agent-a')
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-runner-binding-save"]').trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('target_env_var MODULO_RESERVED is not allowed')
  })

  it('falls back to the generic save-failed copy when the server error carries no detail', async () => {
    bindingsByAgent = { 'agent-a': [row('API_KEY', 'backend-1')] }
    mockPut.mockResolvedValue({ data: null, error: {} })
    const wrapper = mountBindings('agent-a')
    await nextTick()

    await wrapper.find('[data-testid="pipeline-editor-runner-binding-save"]').trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Saving bindings failed')
    expect(wrapper.text()).not.toContain('undefined')
  })

  it('adopts the server payload as the binding rows on a successful save', async () => {
    bindingsByAgent = { 'agent-a': [] }
    const serverPayload = [row('API_KEY', 'backend-1')]
    mockPut.mockResolvedValue({ data: { items: serverPayload }, error: undefined })
    const wrapper = mountBindings('agent-a')
    await nextTick()

    await selectBackend(wrapper, 'backend-1')
    await wrapper
      .find('[data-testid="pipeline-editor-runner-binding-target-input"]')
      .setValue('API_KEY')
    await wrapper.find('[data-testid="pipeline-editor-runner-binding-add"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="pipeline-editor-runner-binding-save"]').trigger('click')
    await nextTick()

    const putCall = mockPut.mock.calls[0]
    expect(putCall[0]).toBe('/api/v1/agents/{agent_id}/bindings')
    const putInit = putCall[1] as { params: { path: { agent_id: string } }; body: { bindings: BindingRow[] } }
    expect(putInit.params.path.agent_id).toBe('agent-a')
    expect(putInit.body.bindings).toEqual([row('API_KEY', 'backend-1')])
    // The rows reflect the canonicalised server payload, and no banner shows.
    const rows = wrapper.findAll('[data-testid="pipeline-editor-runner-bindings-rows"] li')
    expect(rows).toHaveLength(1)
    expect(wrapper.text()).toContain('API_KEY')
    expect(wrapper.text()).not.toContain('Saving bindings failed')
  })
})
