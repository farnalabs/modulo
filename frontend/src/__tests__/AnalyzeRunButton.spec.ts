import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import AnalyzeRunButton from '../components/runs/AnalyzeRunButton.vue'
import { useAssistantStore } from '../composables/useAssistantStore'
import { usePlanStore } from '../stores/planStore'
import type { AnalyzeRunInfo } from '../components/runs/analyzeRun'

const apiMocks = vi.hoisted(() => ({
  modelBackends: vi.fn(),
  createSession: vi.fn(),
  sessionBody: { body: null as Record<string, unknown> | null },
}))

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
  api: {
    GET: vi.fn((url: string) => {
      if (url === '/api/v1/model-backends') return apiMocks.modelBackends()
      return Promise.resolve({ data: null, error: undefined })
    }),
    POST: vi.fn((url: string, options?: { body?: Record<string, unknown> }) => {
      if (url === '/api/v1/assistant/sessions') {
        apiMocks.sessionBody.body = options?.body ?? null
        return apiMocks.createSession()
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
    PATCH: vi.fn(() => Promise.resolve({ data: null, error: undefined })),
    DELETE: vi.fn(() => Promise.resolve({ data: null, error: undefined })),
  },
}))

const streamMocks = vi.hoisted(() => ({
  connectStream: vi.fn(() => Promise.resolve()),
}))

vi.mock('@/composables/useAssistantStream', () => ({
  useAssistantStream: vi.fn(() => ({
    connectStream: streamMocks.connectStream,
    disconnectStream: vi.fn(() => Promise.resolve()),
    connected: { value: false },
  })),
}))

function failedRun(overrides: Partial<AnalyzeRunInfo> = {}): AnalyzeRunInfo {
  return {
    runId: 'run-42',
    runNumber: 42,
    pipelineId: 'pipe-1',
    pipelineName: 'Deploy pipeline',
    status: 'failed',
    errorCode: 'harness.worker_failed',
    errorDetail: 'node "build" exited with code 1',
    failingNode: 'build',
    ...overrides,
  }
}

function enableAssistant() {
  const plan = usePlanStore()
  plan.features = { assistant: true }
  plan.devMode = true
  plan.loaded = true
  return plan
}

function modelBackends(items: Array<{ has_credentials: boolean }>) {
  apiMocks.modelBackends.mockResolvedValue({ data: { items }, error: undefined })
}

function createSessionResponse() {
  apiMocks.createSession.mockResolvedValue({
    data: {
      id: 'session-9',
      session_number: 9,
      name: 'Analyze run #42',
      provider: 'openai',
      model: 'gpt-4o',
      context_window_tokens: 200000,
      updated_at: new Date().toISOString(),
    },
    error: undefined,
  })
}

function mountButton(run: AnalyzeRunInfo = failedRun()) {
  return mount(AnalyzeRunButton, { props: { run } })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  setActivePinia(createPinia())
  apiMocks.sessionBody.body = null
  apiMocks.modelBackends.mockResolvedValue({ data: { items: [] }, error: undefined })
  createSessionResponse()
})

describe('AnalyzeRunButton visibility', () => {
  it('does not render while the assistant is still unconfigured-plan (flag off / no dev mode)', async () => {
    const wrapper = mountButton()
    await flushPromises()
    expect(wrapper.find('[data-testid="run-detail-analyze"]').exists()).toBe(false)
  })

  it('does not render for a successful run even when the assistant is enabled', async () => {
    enableAssistant()
    const wrapper = mountButton(failedRun({ status: 'complete', errorCode: null, errorDetail: null }))
    await flushPromises()
    expect(wrapper.find('[data-testid="run-detail-analyze"]').exists()).toBe(false)
  })

  it('does not render for a user-cancelled run even when the assistant is enabled', async () => {
    enableAssistant()
    const wrapper = mountButton(failedRun({ status: 'cancelled' }))
    await flushPromises()
    expect(wrapper.find('[data-testid="run-detail-analyze"]').exists()).toBe(false)
  })

  it.each(['failed', 'eval_failed', 'stalled', 'budget_exceeded'] as const)(
    'renders for the %s failure state when the assistant is enabled',
    async (status) => {
      enableAssistant()
      const wrapper = mountButton(failedRun({ status }))
      await flushPromises()
      expect(wrapper.find('[data-testid="run-detail-analyze-button"]').exists()).toBe(true)
    },
  )
})

describe('AnalyzeRunButton configured / disabled states', () => {
  it('shows a disabled, focusable button with the not-configured tooltip when no backend has credentials', async () => {
    enableAssistant()
    modelBackends([{ has_credentials: false }])
    const wrapper = mountButton()
    await flushPromises()

    const button = wrapper.find('[data-testid="run-detail-analyze-button"]')
    expect(button.exists()).toBe(true)
    expect(button.attributes('aria-disabled')).toBe('true')
    // Native `disabled` would remove the button from the tab order; aria-disabled
    // keeps it focusable so the tooltip stays reachable from the keyboard.
    expect(button.attributes('disabled')).toBeUndefined()

    const tooltip = wrapper.find('[data-testid="run-detail-analyze-tooltip"]')
    expect(tooltip.exists()).toBe(true)
    expect(tooltip.attributes('role')).toBe('tooltip')
    expect(tooltip.text()).toContain('not configured')
    expect(button.attributes('aria-describedby')).toBe(tooltip.attributes('id'))
  })

  it('shows the disabled tooltip while the configuration check is still in flight', async () => {
    enableAssistant()
    apiMocks.modelBackends.mockReturnValue(new Promise(() => {}))
    const wrapper = mountButton()
    await flushPromises()

    const button = wrapper.find('[data-testid="run-detail-analyze-button"]')
    expect(button.attributes('aria-disabled')).toBe('true')
    expect(wrapper.find('[data-testid="run-detail-analyze-tooltip"]').text()).toContain('Checking')
  })

  it('enables the button when at least one model backend holds credentials', async () => {
    enableAssistant()
    modelBackends([{ has_credentials: false }, { has_credentials: true }])
    const wrapper = mountButton()
    await flushPromises()

    const button = wrapper.find('[data-testid="run-detail-analyze-button"]')
    expect(button.attributes('aria-disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="run-detail-analyze-tooltip"]').exists()).toBe(false)
  })

  it('fails closed when the model-backends check errors', async () => {
    enableAssistant()
    apiMocks.modelBackends.mockResolvedValue({ data: null, error: { status: 403, detail: 'forbidden' } })
    const wrapper = mountButton()
    await flushPromises()

    const button = wrapper.find('[data-testid="run-detail-analyze-button"]')
    expect(button.attributes('aria-disabled')).toBe('true')
    expect(wrapper.find('[data-testid="run-detail-analyze-tooltip"]').text()).toContain('not configured')
  })

  it('does not query model backends at all when the assistant is disabled', async () => {
    const wrapper = mountButton()
    await flushPromises()
    expect(wrapper.find('[data-testid="run-detail-analyze"]').exists()).toBe(false)
    expect(apiMocks.modelBackends).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('AnalyzeRunButton press', () => {
  it('creates a seeded session, opens the panel, and starts the stream', async () => {
    enableAssistant()
    modelBackends([{ has_credentials: true }])
    const wrapper = mountButton()
    await flushPromises()

    await wrapper.find('[data-testid="run-detail-analyze-button"]').trigger('click')
    await flushPromises()

    // Session is created titled with the run label.
    expect(apiMocks.createSession).toHaveBeenCalledTimes(1)
    expect(apiMocks.sessionBody.body).toMatchObject({ name: 'Analyze run #42' })
    const store = useAssistantStore()
    expect(store.activeSessionId).toBe('session-9')
    expect(store.sessions[0]?.name).toBe('Analyze run #42')

    // The seeded first message carries the run identity, the error, and the RCA request.
    const seeded = store.messages[0]
    expect(seeded.role).toBe('user')
    const seededContent = seeded.content ?? ''
    expect(seededContent).toContain('#42')
    expect(seededContent).toContain('Deploy pipeline')
    expect(seededContent).toContain('harness.worker_failed')
    expect(seededContent).toContain('node "build" exited with code 1')
    expect(seededContent).toContain('build')
    expect(seededContent.toLowerCase()).toContain('root cause')

    // The conversation is surfaced and streamed.
    expect(store.panelState).toBe('floating')
    expect(streamMocks.connectStream).toHaveBeenCalledWith('session-9')

    // aria-live status line reports the handoff.
    const status = wrapper.find('[data-testid="run-detail-analyze-status"]')
    expect(status.attributes('role')).toBe('status')
    expect(status.text()).toContain('Root-cause analysis started')
  })

  it('reports a failure without throwing when the session cannot be created', async () => {
    enableAssistant()
    modelBackends([{ has_credentials: true }])
    apiMocks.createSession.mockResolvedValue({ data: null, error: { status: 500, detail: 'boom' } })
    const wrapper = mountButton()
    await flushPromises()

    await wrapper.find('[data-testid="run-detail-analyze-button"]').trigger('click')
    await flushPromises()

    expect(streamMocks.connectStream).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="run-detail-analyze-status"]').text()).toContain('Failed to start')
  })

  it('blocks a second press while an assistant response is already streaming', async () => {
    enableAssistant()
    modelBackends([{ has_credentials: true }])
    const store = useAssistantStore()
    store.isStreaming = true
    const wrapper = mountButton()
    await flushPromises()

    const button = wrapper.find('[data-testid="run-detail-analyze-button"]')
    expect(button.attributes('aria-disabled')).toBe('true')
    expect(wrapper.find('[data-testid="run-detail-analyze-tooltip"]').text()).toContain('already responding')

    await button.trigger('click')
    await flushPromises()
    expect(apiMocks.createSession).not.toHaveBeenCalled()
  })
})
