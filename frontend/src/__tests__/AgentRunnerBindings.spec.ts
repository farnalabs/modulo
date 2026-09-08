import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { api } from '../lib/api/client'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    PUT: vi.fn(),
    POST: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AgentRunnerBindings from '../components/agent/AgentRunnerBindings.vue'

interface BindingRow {
  model_backend_id: string
  target_env_var: string
  source_field: string
}

interface RunnerBindingsVm {
  bindings: BindingRow[]
  newBackendId: string | null
  newTargetVar: string
  error: string
  addRow: () => void
  save: () => Promise<void>
}

const MB_ITEMS = [
  { id: 'mb-1', name: 'OpenAI', provider: 'openai' },
  { id: 'mb-2', name: 'Anthropic', provider: 'anthropic' },
]

function mockGet(overrides: Partial<Record<string, unknown>> = {}) {
  ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
    if (path === '/api/v1/model-backends') {
      return Promise.resolve({ data: { items: MB_ITEMS }, error: undefined })
    }
    if (path.startsWith('/api/v1/agents/') && path.endsWith('/bindings')) {
      return Promise.resolve({ data: { items: [] }, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
  Object.assign((api.GET as ReturnType<typeof vi.fn>).getMockImplementation() ?? {}, overrides)
}

describe('AgentRunnerBindings (FAR-592 F5)', () => {
  beforeEach(() => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockReset()
    ;(api.PUT as ReturnType<typeof vi.fn>).mockReset()
    ;(api.POST as ReturnType<typeof vi.fn>).mockReset()
  })
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('loads model backends + bindings on mount for the given agent', async () => {
    mockGet()
    const wrapper = mount(AgentRunnerBindings, {
      props: { agentId: 'agent-1' },
    })
    await flushPromises()

    expect(api.GET).toHaveBeenCalledWith('/api/v1/model-backends')
    expect(api.GET).toHaveBeenCalledWith('/api/v1/agents/{agent_id}/bindings', {
      params: { path: { agent_id: 'agent-1' } },
    })
    expect((wrapper.vm as unknown as RunnerBindingsVm).bindings).toEqual([])
  })

  it('does not load when agentId is absent', async () => {
    mockGet()
    mount(AgentRunnerBindings, { props: { agentId: null } })
    await flushPromises()
    expect(api.GET).not.toHaveBeenCalled()
  })

  it('resets prior agent state and reloads when agentId changes (watch)', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((path: string, opts?: { params?: { path?: { agent_id?: string } } }) => {
      if (path === '/api/v1/model-backends') {
        return Promise.resolve({ data: { items: MB_ITEMS }, error: undefined })
      }
      if (path.startsWith('/api/v1/agents/')) {
        // Return a populated binding for the first agent so stale state exists.
        const items =
          opts?.params?.path?.agent_id === 'agent-1'
            ? [{ model_backend_id: 'mb-1', target_env_var: 'OLD_KEY', source_field: 'api_key' }]
            : []
        return Promise.resolve({ data: { items }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(AgentRunnerBindings, {
      props: { agentId: 'agent-1' },
    })
    const vm = wrapper.vm as unknown as RunnerBindingsVm
    await flushPromises()
    expect(vm.bindings).toHaveLength(1)

    await wrapper.setProps({ agentId: 'agent-2' })
    await flushPromises()

    // Outgoing agent's rows are cleared before the reload.
    expect(vm.bindings).toEqual([])
    expect(api.GET).toHaveBeenLastCalledWith('/api/v1/agents/{agent_id}/bindings', {
      params: { path: { agent_id: 'agent-2' } },
    })
  })

  it('adds a row only when valid and rejects case-insensitive duplicates client-side', async () => {
    mockGet()
    const wrapper = mount(AgentRunnerBindings, {
      props: { agentId: 'agent-1' },
    })
    const vm = wrapper.vm as unknown as RunnerBindingsVm
    await flushPromises()

    vm.newBackendId = 'mb-1'
    vm.newTargetVar = 'API_KEY'
    vm.addRow()
    await nextTick()
    expect(vm.bindings).toHaveLength(1)

    // A differently-cased duplicate is caught client-side before reaching the server.
    vm.newBackendId = 'mb-2'
    vm.newTargetVar = 'api_key'
    vm.addRow()
    await nextTick()
    expect(vm.bindings).toHaveLength(1)
    expect(vm.error).toContain('API_KEY')
  })

  it('surfaces the server error detail on a failed save instead of swallowing it', async () => {
    mockGet()
    ;(api.PUT as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: null,
      error: { detail: 'bindings accept org-visible backends only' },
    })
    const wrapper = mount(AgentRunnerBindings, {
      props: { agentId: 'agent-1' },
    })
    const vm = wrapper.vm as unknown as RunnerBindingsVm
    await flushPromises()

    await vm.save()
    await flushPromises()
    expect(vm.error).toBe('bindings accept org-visible backends only')
  })

  it('updates bindings from the server payload on a successful save', async () => {
    mockGet()
    ;(api.PUT as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { items: [{ model_backend_id: 'mb-1', target_env_var: 'API_KEY', source_field: 'api_key' }] },
      error: undefined,
    })
    const wrapper = mount(AgentRunnerBindings, {
      props: { agentId: 'agent-1' },
    })
    const vm = wrapper.vm as unknown as RunnerBindingsVm
    await flushPromises()

    vm.newBackendId = 'mb-1'
    vm.newTargetVar = 'API_KEY'
    vm.addRow()
    await nextTick()
    await vm.save()
    await flushPromises()
    expect(vm.bindings).toHaveLength(1)
    expect(vm.error).toBe('')
    expect(api.PUT).toHaveBeenCalledWith('/api/v1/agents/{agent_id}/bindings', {
      params: { path: { agent_id: 'agent-1' } },
      body: { bindings: [{ model_backend_id: 'mb-1', target_env_var: 'API_KEY', source_field: 'api_key' }] },
    })
  })
})
