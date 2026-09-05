import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

let mockRunStatus = 'complete'
let mockInputPayload: Record<string, unknown> | null = null
let mockTriggerActor: string | null = null
let mockTriggerType: string | null = 'manual'
let mockHeartbeatAt: string | null = null
let mockWorkItemRefs: unknown = null
let mockChildRuns: unknown = null
let mockCapacity: unknown = null
let mockPendingGates: Array<Record<string, unknown>> = []
let mockWorkspaceLease: Record<string, unknown> | null = null
let mockNodeLabels: Record<string, string> | null = null
let mockClaimResult: Record<string, unknown> | null = null
let mockClaimError: Record<string, unknown> | undefined
let mockApproveResult: Record<string, unknown> | null = null
let mockApproveError: Record<string, unknown> | undefined
let mockRejectResult: Record<string, unknown> | null = null
let mockRejectError: Record<string, unknown> | undefined

vi.mock('../lib/api/client', () => {
  const mockPost = vi.fn().mockImplementation((url: string) => {
    if (url === '/api/v1/runs/{run_id}/nodes/{node_id}/prompt/reveal') {
      return Promise.resolve({
        data: {
          prompt: '<SYSTEM>\\nYou are a helpful assistant.\\n</SYSTEM>\\n\\n<USER>\\n{"query": "hello"}\\n</USER>',
          messages: [
            { role: 'system', content: 'You are a helpful assistant.' },
            { role: 'user', content: '{"query": "hello"}' }
          ],
          token_count: 25,
          prompt_always_visible: false
        },
        error: undefined
      })
    }
    if (url === '/api/v1/runs/{run_id}/hitl/{gate_id}/claim') {
      if (mockClaimError) return Promise.resolve({ data: null, error: mockClaimError })
      return Promise.resolve({ data: mockClaimResult, error: undefined })
    }
    if (url === '/api/v1/runs/{run_id}/hitl/{gate_id}/approve') {
      if (mockApproveError) return Promise.resolve({ data: null, error: mockApproveError })
      return Promise.resolve({ data: mockApproveResult, error: undefined })
    }
    if (url === '/api/v1/runs/{run_id}/hitl/{gate_id}/reject') {
      if (mockRejectError) return Promise.resolve({ data: null, error: mockRejectError })
      return Promise.resolve({ data: mockRejectResult, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
  return {
    api: {
      GET: vi.fn().mockImplementation((url: string) => {
        if (url === '/api/v1/runs/{run_id}') {
          return Promise.resolve({
            data: {
              run_id: 'test-run-id',
              pipeline_id: 'test-pipeline',
              status: mockRunStatus,
              total_cost_usd: 1.23,
              token_consumption: null,
              node_token_usage: { 'node-a': { input_tokens: 10, output_tokens: 20, total_tokens: 30 } },
              trace_id: null,
              trigger_type: mockTriggerType,
              trigger_actor: mockTriggerActor,
              heartbeat_at: mockHeartbeatAt,
              work_item_refs: mockWorkItemRefs,
              child_runs: mockChildRuns,
              capacity: mockCapacity,
            },
            error: undefined
          })
        }
        if (url === '/api/v1/runs/{run_id}/io') {
          return Promise.resolve({
            data: {
              outputs_json: { 'node-a': { input: { q: 'hello' }, output: 'response' } },
              input_payload: mockInputPayload,
              node_labels: mockNodeLabels,
              node_telemetry: {
                'node-a': {
                  status: 'complete',
                  exit_code: 0,
                  wall_clock_time_ms: 12345,
                  cost_estimate_usd: 0.0123,
                  agent_stdout: 'Building widget...\nWidget built.\n',
                  agent_stderr: 'Warning: legacy config detected.\n',
                  stall_reason: 'waiting on upstream',
                  summary: 'telemetry-level summary',
                },
              },
            },
            error: undefined,
          })
        }
        if (url === '/api/v1/runs/{run_id}/hitl/pending') {
          return Promise.resolve({ data: { gates: mockPendingGates }, error: undefined })
        }
        if (url === '/api/v1/runs/{run_id}/workspace-lease') {
          return Promise.resolve({ data: mockWorkspaceLease, error: undefined })
        }
        return Promise.resolve({ data: null, error: undefined })
      }),
      POST: mockPost
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token')
  }
})

const testRoute = vi.hoisted(() => ({
  params: { id: 'test-run-id' },
  fullPath: '/runs/test-run-id',
  path: '/runs/test-run-id',
  query: {},
  hash: '',
  matched: [],
  name: 'run-detail',
  redirectedFrom: undefined,
  meta: {},
} as const))

vi.mock('vue-router', () => {
  const mockRouter = {
    push: vi.fn().mockResolvedValue(undefined),
    replace: vi.fn(),
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    onError: vi.fn(),
    currentRoute: { value: testRoute },
    getRoutes: vi.fn(() => []),
    addRoute: vi.fn(),
    removeRoute: vi.fn(),
    hasRoute: vi.fn(() => false),
    isReady: vi.fn().mockResolvedValue(undefined),
    install: vi.fn(),
  }
  return {
    useRoute: vi.fn(() => testRoute),
    useRouter: vi.fn(() => mockRouter),
    createRouter: vi.fn(() => mockRouter),
    createWebHistory: vi.fn(() => ({})),
  }
})

import RunDetailView from '../views/RunDetailView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/runs/:id', name: 'run-detail', component: RunDetailView }
  ]
})

describe('RunDetailView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockRunStatus = 'complete'
    mockInputPayload = null
    mockTriggerActor = null
    mockTriggerType = 'manual'
    mockHeartbeatAt = null
    mockWorkItemRefs = null
    mockChildRuns = null
    mockCapacity = null
    mockPendingGates = []
    mockWorkspaceLease = null
    mockNodeLabels = null
    mockClaimResult = null
    mockClaimError = undefined
    mockApproveResult = null
    mockApproveError = undefined
    mockRejectResult = null
    mockRejectError = undefined
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  function createWrapper() {
    const div = document.createElement('div')
    div.id = 'root'
    document.body.appendChild(div)
    return mount(RunDetailView, {
      global: { plugins: [router] },
      attachTo: div
    })
  }

  it('renders without crashing', async () => {
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Run Detail')
    wrapper.unmount()
  })

  it('shows a single view-prompt button for each node', async () => {
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()
    const promptBtns = wrapper.findAll('[data-testid="run-detail-show-prompt"]')
    expect(promptBtns.length).toBeGreaterThanOrEqual(1)
    expect(promptBtns[0].text()).toContain('View prompt')
    wrapper.unmount()
  })

  it('reveals the prompt and opens the dialog on a single click', async () => {
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    const promptBtn = wrapper.find('[data-testid="run-detail-show-prompt"]')
    expect(promptBtn.exists()).toBe(true)
    await promptBtn.trigger('click')
    await nextTick()
    await flushPromises()

    expect(document.body.textContent).toContain('helpful assistant')
    expect(document.body.textContent).toContain('25')
    wrapper.unmount()
  })

  it('copies prompt text from dialog', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    const promptBtn = wrapper.find('[data-testid="run-detail-show-prompt"]')
    await promptBtn.trigger('click')
    await nextTick()
    await flushPromises()

    const copyBtn = document.querySelector('[data-testid="run-detail-copy-prompt"]')
    expect(copyBtn).not.toBeNull()
    ;(copyBtn as HTMLElement).click()
    expect(writeText).toHaveBeenCalled()
    expect(writeText.mock.calls[0][0]).toContain('helpful assistant')
    wrapper.unmount()
  })

  it('shows node input and output values from normalized outputs_json', async () => {
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()
    // Complete runs auto-expand the last node's IO row.
    expect(wrapper.text()).toContain('hello')
    expect(wrapper.text()).toContain('response')
    wrapper.unmount()
  })

  it('shows empty-state messages instead of the string null for a node with no input/output', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({ data: { ...baseDetail(), node_token_usage: { 'node-a': { input_tokens: 10, output_tokens: 20, total_tokens: 30 } } }, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: { 'node-a': {} } }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    const noInput = wrapper.find('[data-testid="run-detail-no-input"]')
    const noOutput = wrapper.find('[data-testid="run-detail-no-output"]')
    expect(noInput.exists()).toBe(true)
    expect(noOutput.exists()).toBe(true)
    expect(noInput.text()).toContain('No input data')
    expect(noOutput.text()).toContain('No output data')
    expect(noInput.text()).not.toContain('null')
    wrapper.unmount()
  })

  it('shows waiting-for-capacity banner for pending capacity-blocked runs', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            run_id: 'test-run-id',
            pipeline_id: 'test-pipeline',
            status: 'pending',
            error_code: 'capacity.org',
            error_detail: 'Org sandbox concurrency limit reached: 3 active, cap 2',
            total_cost_usd: null,
            token_consumption: null,
            node_token_usage: null,
            trace_id: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    const banner = wrapper.find('[data-testid="run-detail-waiting-for-capacity"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Waiting for capacity')
    expect(banner.text()).toContain('Org sandbox concurrency limit reached: 3 active, cap 2')
    wrapper.unmount()
  })

  it('shows an error-code badge on the failed-run diagnostics panel', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            run_id: 'test-run-id',
            pipeline_id: 'test-pipeline',
            status: 'failed',
            error_code: 'harness.worker_failed',
            error_detail: 'boom: worker crashed',
            total_cost_usd: null,
            token_consumption: null,
            node_token_usage: null,
            trace_id: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Run Error')
    expect(wrapper.text()).toContain('Worker failed')
    expect(wrapper.text()).toContain('boom: worker crashed')
    wrapper.unmount()
  })

  async function mockRunDetail(data: Record<string, unknown>) {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({ data, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
  }

  function baseDetail() {
    return {
      run_id: 'test-run-id',
      pipeline_id: 'test-pipeline',
      status: 'complete',
      total_cost_usd: 1.23,
      token_consumption: null,
      node_token_usage: null,
      trace_id: null,
    }
  }

  async function mountWithDetail(data: Record<string, unknown>) {
    await mockRunDetail(data)
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()
    return wrapper
  }

  function normalizedIOData() {
    return {
      outputs_json: { 'node-a': { result: 'pure agent return', summary: 'return-level summary' } },
      node_telemetry: {
        'node-a': {
          status: 'complete',
          exit_code: 0,
          wall_clock_time_ms: 12345,
          cost_estimate_usd: 0.0123,
          agent_stdout: 'Building widget...\nWidget built.\n',
          agent_stderr: 'Warning: legacy config detected.\n',
          stall_reason: 'waiting on upstream',
          summary: 'telemetry-level summary',
        },
      },
    }
  }

  async function mountWithNormalizedIO() {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            run_id: 'test-run-id',
            pipeline_id: 'test-pipeline',
            status: 'complete',
            total_cost_usd: 1.23,
            token_consumption: null,
            node_token_usage: { 'node-a': { input_tokens: 10, output_tokens: 20, total_tokens: 30 } },
            trace_id: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: normalizedIOData(), error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('shows aggregate cost line when child runs exist', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      child_runs_cost_usd: '0.25',
      aggregate_cost_usd: '1.48',
    })
    const aggregate = wrapper.find('[data-testid="run-detail-aggregate-cost"]')
    expect(aggregate.exists()).toBe(true)
    expect(aggregate.text()).toContain('1.480000')
    expect(aggregate.text()).toContain('0.250000')
    // Count absent → generic wording, no count interpolation.
    expect(aggregate.text()).toContain('Total including child runs:')
    wrapper.unmount()
  })

  it('shows child run count in the aggregate line when available', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      child_runs_cost_usd: '0.25',
      aggregate_cost_usd: '1.48',
      child_runs_count: 3,
    })
    const aggregate = wrapper.find('[data-testid="run-detail-aggregate-cost"]')
    expect(aggregate.exists()).toBe(true)
    expect(aggregate.text()).toContain('Total including 3 child runs:')
    wrapper.unmount()
  })

  it('falls back to generic aggregate wording when the count is zero', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      child_runs_cost_usd: '0.25',
      aggregate_cost_usd: '1.48',
      child_runs_count: 0,
    })
    const aggregate = wrapper.find('[data-testid="run-detail-aggregate-cost"]')
    expect(aggregate.exists()).toBe(true)
    expect(aggregate.text()).toContain('Total including child runs:')
    wrapper.unmount()
  })

  it('shows no aggregate line when there are no child runs', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      child_runs_cost_usd: '0.000000',
      aggregate_cost_usd: '1.230000',
    })
    expect(wrapper.find('[data-testid="run-detail-aggregate-cost"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows no aggregate line when rollup fields are absent', async () => {
    const wrapper = await mountWithDetail(baseDetail())
    expect(wrapper.find('[data-testid="run-detail-aggregate-cost"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each(['pending', 'running', 'awaiting_human', 'claimed'])('shows the cancel button for %s runs', async (status) => {
    const wrapper = await mountWithDetail({ ...baseDetail(), status })
    expect(wrapper.find('[data-testid="run-detail-cancel"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it.each(['complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded'])('hides the cancel button for %s runs', async (status) => {
    const wrapper = await mountWithDetail({ ...baseDetail(), status })
    expect(wrapper.find('[data-testid="run-detail-cancel"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('calls the cancel endpoint on click and flips the run to cancelled', async () => {
    const { api } = await import('../lib/api/client')
    const wrapper = await mountWithDetail({ ...baseDetail(), status: 'running' })
    const cancelBtn = wrapper.find('[data-testid="run-detail-cancel"]')
    await cancelBtn.trigger('click')
    await flushPromises()
    await nextTick()
    expect(api.POST).toHaveBeenCalledWith('/api/v1/runs/{run_id}/cancel', {
      params: { path: { run_id: 'test-run-id' } },
    })
    expect(wrapper.find('[data-testid="run-detail-cancel"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('cancelled')
    wrapper.unmount()
  })

  it('shows an inline error when the detail cancel request fails', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({ data: null, error: { detail: 'run_already_terminal' } })
    const wrapper = await mountWithDetail({ ...baseDetail(), status: 'running' })
    await wrapper.find('[data-testid="run-detail-cancel"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Failed to cancel:')
    expect(wrapper.text()).toContain('run_already_terminal')
    expect(wrapper.find('[data-testid="run-detail-cancel"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders the PURE agent return in the IO output tree with no telemetry keys', async () => {
    const wrapper = await mountWithNormalizedIO()

    const toggleIo = wrapper.find('[data-testid="run-detail-toggle-io"]')
    expect(toggleIo.exists()).toBe(true)
    await toggleIo.trigger('click')
    await nextTick()

    const ioRow = wrapper.find('[data-testid="run-detail-io-row"]')
    expect(ioRow.exists()).toBe(true)
    // The pure return has no explicit `input` key, so the input panel renders
    // as an empty state and only the output panel gets a JsonViewer.
    expect(wrapper.find('[data-testid="run-detail-no-input"]').exists()).toBe(true)
    const viewers = ioRow.findAll('[data-testid="json-viewer"]')
    expect(viewers.length).toBe(1)
    const outputPanel = viewers[0]
    expect(outputPanel.text()).toContain('pure agent return')
    expect(outputPanel.text()).toContain('return-level summary')
    expect(outputPanel.text()).not.toContain('agent_stdout')
    expect(outputPanel.text()).not.toContain('agent_stderr')
    expect(outputPanel.text()).not.toContain('exit_code')
    expect(outputPanel.text()).not.toContain('stall_reason')
    expect(outputPanel.text()).not.toContain('wall_clock_time')
    wrapper.unmount()
  })

  it('renders agent telemetry/logs (stdout, stderr, stall_reason) in the Logs section', async () => {
    const wrapper = await mountWithNormalizedIO()

    const toggleLogs = wrapper.find('[data-testid="run-detail-toggle-logs"]')
    expect(toggleLogs.exists()).toBe(true)
    await toggleLogs.trigger('click')
    await nextTick()

    const logRow = wrapper.find('[data-testid="run-detail-log-row"]')
    expect(logRow.exists()).toBe(true)
    const text = logRow.text()
    expect(text).toContain('Agent Telemetry')
    expect(text).toContain('Building widget...')
    expect(text).toContain('Warning: legacy config detected.')
    expect(text).toContain('Agent stalled: waiting on upstream')
    expect(text).toContain('return-level summary')
    // Exit code 0 (success) does not render an "Exit code" chip.
    expect(text).not.toContain('Exit code')
    wrapper.unmount()
  })

  it('renders the exit-code chip when the telemetry exit code is non-zero', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({ data: baseDetail(), error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: {
            outputs_json: null,
            node_telemetry: {
              'node-a': { status: 'failed', exit_code: 2, agent_stderr: 'boom' },
            },
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()

    const logRow = wrapper.find('[data-testid="run-detail-log-row"]')
    expect(logRow.text()).toContain('Exit code: 2')
    wrapper.unmount()
  })

  it('shows the telemetry card for nodes that have telemetry but no stdout/stderr logs', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({ data: baseDetail(), error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: {
            outputs_json: { 'gate-a': { result: 'passed' } },
            node_telemetry: {
              'gate-a': {
                status: 'complete',
                exit_code: 0,
                wall_clock_time_ms: 99,
                cost_estimate_usd: 0.0001,
                summary: 'gate ran cleanly',
              },
            },
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    const toggleLogs = wrapper.find('[data-testid="run-detail-toggle-logs"]')
    expect(toggleLogs.exists()).toBe(true)
    await toggleLogs.trigger('click')
    await nextTick()

    const telemetry = wrapper.find('[data-testid="run-detail-node-telemetry"]')
    expect(telemetry.exists()).toBe(true)
    expect(telemetry.text()).toContain('Agent Telemetry')
    expect(telemetry.text()).toContain('99')
    expect(telemetry.text()).toContain('gate ran cleanly')
    wrapper.unmount()
  })

  it('prefers the pure return summary over telemetry summary for non-failed nodes', async () => {
    const wrapper = await mountWithNormalizedIO()

    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()

    const summary = wrapper.find('[data-testid="run-detail-node-summary"]')
    expect(summary.exists()).toBe(true)
    expect(summary.text()).toContain('return-level summary')
    expect(summary.text()).not.toContain('telemetry-level summary')
    wrapper.unmount()
  })

  it('falls back to the telemetry summary when the node status is failed', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({ data: baseDetail(), error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: {
            outputs_json: { 'node-a': { result: 'partial return', summary: 'return-level summary' } },
            node_telemetry: {
              'node-a': { status: 'failed', exit_code: 1, agent_stderr: 'boom', summary: 'telemetry-level summary' },
            },
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()

    const summary = wrapper.find('[data-testid="run-detail-node-summary"]')
    expect(summary.exists()).toBe(true)
    expect(summary.text()).toContain('telemetry-level summary')
    wrapper.unmount()
  })

  it('legacy envelope outputs_json (no node_telemetry) splits input and output across the IO panels', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({ data: baseDetail(), error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: {
            outputs_json: { 'node-a': { input: { q: 'hello' }, output: { result: 'legacy response', agent_stdout: 'legacy stdout' } } },
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    const toggleIo = wrapper.find('[data-testid="run-detail-toggle-io"]')
    expect(toggleIo.exists()).toBe(true)
    await toggleIo.trigger('click')
    await nextTick()

    const ioRow = wrapper.find('[data-testid="run-detail-io-row"]')
    const viewers = ioRow.findAll('[data-testid="json-viewer"]')
    expect(viewers.length).toBe(2)
    const inputPanel = viewers[0]
    expect(inputPanel.text()).toContain('hello')
    const outputPanel = viewers[1]
    expect(outputPanel.text()).toContain('legacy response')
    expect(outputPanel.text()).toContain('legacy stdout')
    expect(outputPanel.text()).toContain('agent_stdout')

    // No node_telemetry -> no Logs toggle, no telemetry card.
    expect(wrapper.find('[data-testid="run-detail-toggle-logs"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="run-detail-node-telemetry"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders a View trace link when trace_url is present', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      trace_id: 'abc123',
      trace_url: 'https://otel.example.com/jaeger/ui/trace/abc123',
    })
    const link = wrapper.find('[data-testid="run-detail-view-trace"]')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('https://otel.example.com/jaeger/ui/trace/abc123')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    expect(link.text()).toContain('View trace')
    wrapper.unmount()
  })

  it('renders no View trace link when trace_url is absent', async () => {
    const wrapper = await mountWithDetail({ ...baseDetail(), trace_id: 'abc123' })
    expect(wrapper.find('[data-testid="run-detail-view-trace"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the per-node span id from node telemetry in the trace column', async () => {
    const { api } = await import('../lib/api/client')
    const originalImpl = (api.GET as any).getMockImplementation()
    try {
      ;(api.GET as any).mockImplementation((url: string) => {
        if (url === '/api/v1/runs/{run_id}') {
          return Promise.resolve({
            data: {
              ...baseDetail(),
              node_token_usage: { 'node-a': { input_tokens: 1, output_tokens: 1, total_tokens: 2 } },
            },
            error: undefined,
          })
        }
        if (url === '/api/v1/runs/{run_id}/io') {
          return Promise.resolve({
            data: {
              outputs_json: { 'node-a': { result: 'ok' } },
              node_telemetry: {
                'node-a': { status: 'complete', otel_span_id: '0123456789abcdef', otel_trace_id: 'zz99' },
              },
            },
            error: undefined,
          })
        }
        return Promise.resolve({ data: null, error: undefined })
      })

      router.push('/runs/test-run-id')
      await router.isReady()
      const wrapper = createWrapper()
      await nextTick()
      await flushPromises()
      await nextTick()

      const nodeTrace = wrapper.find('[data-testid="run-detail-node-trace-id"]')
      expect(nodeTrace.exists()).toBe(true)
      expect(nodeTrace.attributes('aria-label')).toBe('Copy node span ID')
      expect(nodeTrace.text()).toContain('#01234567')
      wrapper.unmount()
    } finally {
      ;(api.GET as any).mockImplementation(originalImpl)
    }
  })

  it('shows the input payload for a running run (parameters provided when scheduled)', async () => {
    const { api } = await import('../lib/api/client')
    mockRunStatus = 'running'
    mockInputPayload = { task: 'fix bug', pr_number: 42 }
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            run_id: 'test-run-id',
            pipeline_id: 'test-pipeline',
            status: mockRunStatus,
            total_cost_usd: 1.23,
            token_consumption: null,
            node_token_usage: null,
            trace_id: null,
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: { outputs_json: null, input_payload: mockInputPayload },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()
    const section = wrapper.find('[data-testid="run-detail-input-payload"]')
    expect(section.exists()).toBe(true)
    expect(section.text()).toContain('Run Input')
    expect(section.text()).toContain('fix bug')
    wrapper.unmount()
  })

  it('running run shows the live node progress strip with a running chip', async () => {
    mockRunStatus = 'running'
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            ...baseDetail(),
            status: 'running',
            node_token_usage: { 'node-a': { input_tokens: 10, output_tokens: 20, total_tokens: 30 } },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: {
            outputs_json: { 'node-a': { output: 'ok' } },
            node_telemetry: { 'node-a': { status: 'complete' } },
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ events: [{ seq: 1, event_type: 'node_started', payload: { node_id: 'node-a' } }] }),
    }))

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    // Let the 3s polling interval fire once so fetchLiveOutput ingests the event.
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    await nextTick()

    const strip = wrapper.find('[data-testid="run-detail-node-progress"]')
    expect(strip.exists()).toBe(true)
    const runningChip = wrapper.find('[data-testid="run-detail-node-progress-node-a"]')
    expect(runningChip.exists()).toBe(true)
    expect(runningChip.attributes('aria-label')).toContain('running')
    expect(runningChip.text()).toContain('running')
    wrapper.unmount()
  })

  it('running run shows the trigger actor in the metadata row', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      status: 'running',
      trigger_type: 'manual',
      trigger_actor: 'Duncan (GitHub)',
    })

    const actor = wrapper.find('[data-testid="run-detail-trigger-actor"]')
    expect(actor.exists()).toBe(true)
    expect(actor.text()).toContain('Triggered by')
    expect(actor.text()).toContain('Duncan (GitHub)')
    wrapper.unmount()
  })

  it('shows a human-readable trigger type label when no actor is resolved', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      status: 'running',
      trigger_type: 'agent_signal',
      trigger_actor: null,
    })
    const row = wrapper.find('[data-testid="run-detail-trigger-actor"]')
    expect(row.text()).toContain('Agent Signal')
    expect(row.text()).not.toContain('agent_signal')
    wrapper.unmount()
  })

  it('running run shows live cost and token totals so far', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      status: 'running',
      node_token_usage: { 'node-a': { input_tokens: 10, output_tokens: 20, total_tokens: 30 } },
    })

    const line = wrapper.find('[data-testid="run-detail-live-cost"]')
    expect(line.exists()).toBe(true)
    expect(line.text()).toContain('Cost so far')
    expect(line.text()).toContain('30')
    expect(line.text()).toContain('tokens')
    wrapper.unmount()
  })

  it('pending run with capacity.waiting shows the queue banner', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      status: 'pending',
      capacity: { active_runs: 3, concurrency_limit: 5, waiting: true },
    })

    const banner = wrapper.find('[data-testid="run-detail-queue-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Queued')
    expect(banner.text()).toContain('3 active')
    expect(banner.text()).toContain('5 limit')
    wrapper.unmount()
  })

  it('run with work_item_refs shows the work items section', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      work_item_refs: [
        { kind: 'github_pr', ref: 'acme/repo#42', source: 'derived', status: 'open' },
        { kind: 'linear', ref: 'FAR-123', source: 'derived' },
      ],
    })

    const section = wrapper.find('[data-testid="run-detail-work-items"]')
    expect(section.exists()).toBe(true)
    expect(section.text()).toContain('Work items')

    const prLink = section.find('[data-testid="run-detail-pr-link-0"]')
    expect(prLink.exists()).toBe(true)
    expect(prLink.attributes('href')).toBe('https://github.com/acme/repo/pull/42')
    expect(prLink.text()).toContain('PR')
    expect(prLink.text()).toContain('#42')
    expect(section.text()).toContain('derived')
    expect(section.text()).toContain('open')

    expect(section.text()).toContain('linear')
    expect(section.text()).toContain('FAR-123')
    wrapper.unmount()
  })

  it('renders a github_pr item without a link when the ref is not URL-derivable', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      work_item_refs: [
        { kind: 'github_pr', ref: '42', source: 'derived' },
      ],
    })

    const section = wrapper.find('[data-testid="run-detail-work-items"]')
    expect(section.find('[data-testid="run-detail-pr-link-0"]').exists()).toBe(false)
    expect(section.text()).toContain('PR')
    expect(section.text()).toContain('#42')
    wrapper.unmount()
  })

  it('copies the run input payload with the copy button', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    mockInputPayload = { task: 'fix bug', pr_number: 42 }
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({ data: { ...baseDetail() }, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: { outputs_json: null, input_payload: mockInputPayload },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    const copyBtn = wrapper.find('[data-testid="run-detail-copy-input"]')
    expect(copyBtn.exists()).toBe(true)
    expect(copyBtn.attributes('type')).toBe('button')
    await copyBtn.trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalled()
    expect(writeText.mock.calls[0][0]).toContain('"pr_number": 42')
    expect(wrapper.find('[data-testid="run-detail-copy-input"]').text()).toContain('Copied!')
    wrapper.unmount()
  })

  it('run with child_runs shows the child runs section with links', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      child_runs: [
        { run_id: 'child-run-1', run_number: 7, status: 'complete', pipeline_name: 'Child Pipeline' },
      ],
    })

    const section = wrapper.find('[data-testid="run-detail-child-runs"]')
    expect(section.exists()).toBe(true)
    expect(section.text()).toContain('Child runs')
    expect(section.text()).toContain('#7')
    expect(section.text()).toContain('complete')
    expect(section.text()).toContain('Child Pipeline')
    const link = wrapper.find('[data-testid="run-detail-child-link-child-run-1"]')
    expect(link.exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders a dash (never a phantom $0.000000) for a missing self-report entry', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      cost_breakdown: [
        {
          component: 'model_cost',
          display_name: 'Model cost',
          source: 'self_reported',
          amount_usd: '0.000000',
          formula_applied: 'reported',
          rate_usd: null,
          basis: 'reported by agent',
          missing_self_report: true,
          missing_self_report_reason: 'agent_not_reported',
        },
      ],
    })

    // The not-reported chip must be present.
    const chip = wrapper.find('[data-testid="run-detail-not-reported"]')
    expect(chip.exists()).toBe(true)

    // The amount cell for the missing-self-report row must render a dash, NEVER
    // a phantom $0.000000 money value. (The string "$0.000000" legitimately
    // appears elsewhere as a static note / sum-of-components, so scope to the row.)
    const rows = wrapper.findAll('tbody tr')
    const targetRow = rows.find((r) => r.text().includes('Model cost'))!
    expect(targetRow.exists()).toBe(true)
    const amountCell = targetRow.findAll('td')[1]
    expect(amountCell.text()).toBe('—')
    wrapper.unmount()
  })

  it('renders the #warnings section when the run carries warnings', async () => {
    const wrapper = await mountWithDetail({
      ...baseDetail(),
      warnings: [
        { code: 'missing_self_report', severity: 'warning', message: 'No model cost was reported by the agent for this run.' },
      ],
    })

    const section = wrapper.find('[data-testid="run-detail-warnings"]')
    expect(section.exists()).toBe(true)
    const warningItem = wrapper.find('[data-testid="run-detail-warning-missing_self_report-0"]')
    expect(warningItem.exists()).toBe(true)
    expect(wrapper.text()).toContain('No model cost was reported')
    wrapper.unmount()
  })

  it('auto-scrolls to #warnings when arriving with ?warn=1', async () => {
    const originalScroll = Element.prototype.scrollIntoView
    const scrollSpy = vi.fn()
    Element.prototype.scrollIntoView = scrollSpy

    Object.assign(testRoute, { query: { warn: '1' } })
    try {
      const wrapper = await mountWithDetail({
        ...baseDetail(),
        warnings: [
          { code: 'missing_self_report', severity: 'warning', message: 'No model cost was reported by the agent for this run.' },
        ],
      })
      // Let the run load, the warnings section render, and the async scroll
      // watcher (which awaits its own nextTick) fire.
      await flushPromises()
      await nextTick()
      await flushPromises()
      await nextTick()

      expect(scrollSpy).toHaveBeenCalled()
      wrapper.unmount()
    } finally {
      Object.assign(testRoute, { query: {} })
      Element.prototype.scrollIntoView = originalScroll
    }
  })
})

// Module-level helpers for the appended describe blocks below (the original
// describe block declares its own local copies of the same names).
function baseDetail() {
  return {
    run_id: 'test-run-id',
    pipeline_id: 'test-pipeline',
    status: 'complete',
    total_cost_usd: 1.23,
    token_consumption: null,
    node_token_usage: null,
    trace_id: null,
  }
}

function createWrapper() {
  const div = document.createElement('div')
  div.id = 'root'
  document.body.appendChild(div)
  return mount(RunDetailView, {
    global: { plugins: [router] },
    attachTo: div
  })
}

// Reset the appended describes' shared mock state before every test in the
// file (the original describe keeps its own equivalent beforeEach).
beforeEach(() => {
  mockPendingGates = []
  mockWorkspaceLease = null
  mockNodeLabels = null
  mockClaimResult = null
  mockClaimError = undefined
  mockApproveResult = null
  mockApproveError = undefined
  mockRejectResult = null
  mockRejectError = undefined
})

describe('RunDetailView HITL gates', () => {
  function gate(overrides: Record<string, unknown> = {}) {
    return {
      gate_id: 'gate-1',
      run_id: 'test-run-id',
      label: 'Review the deploy plan',
      claimed_by: null,
      ...overrides,
    }
  }

  async function mountAwaiting() {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({ data: { ...baseDetail(), status: 'awaiting_human' }, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/hitl/pending') {
        return Promise.resolve({ data: { gates: mockPendingGates }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    // Re-arm the HITL POST branches: earlier tests in the file replace the
    // api.POST implementation wholesale (e.g. the cancel-error test), which
    // would otherwise leak into these claim/approve/reject flows.
    ;(api.POST as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}/hitl/{gate_id}/claim') {
        if (mockClaimError) return Promise.resolve({ data: null, error: mockClaimError })
        return Promise.resolve({ data: mockClaimResult, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/hitl/{gate_id}/approve') {
        if (mockApproveError) return Promise.resolve({ data: null, error: mockApproveError })
        return Promise.resolve({ data: mockApproveResult, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/hitl/{gate_id}/reject') {
        if (mockRejectError) return Promise.resolve({ data: null, error: mockRejectError })
        return Promise.resolve({ data: mockRejectResult, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('renders the HITL gate section with the gate label and claim button', async () => {
    mockPendingGates = [gate()]
    const wrapper = await mountAwaiting()
    expect(wrapper.text()).toContain('HITL Gate')
    expect(wrapper.text()).toContain('Review the deploy plan')
    const claimBtn = wrapper.find('[data-testid="run-detail-claim-gate"]')
    expect(claimBtn.exists()).toBe(true)
    expect(claimBtn.text()).toContain('Claim Gate')
    wrapper.unmount()
  })

  it('claims a gate and reveals the approve/reject actions with notes', async () => {
    mockPendingGates = [gate()]
    mockClaimResult = { claim_token: 'ct-123' }
    const { api } = await import('../lib/api/client')
    const wrapper = await mountAwaiting()

    await wrapper.find('[data-testid="run-detail-claim-gate"]').trigger('click')
    await flushPromises()
    await nextTick()

    const post = (api.POST as any).mock.calls.find((c: unknown[]) => c[0] === '/api/v1/runs/{run_id}/hitl/{gate_id}/claim')
    expect(post).toBeTruthy()
    expect((post as unknown[])[1]).toEqual({
      params: { path: { run_id: 'test-run-id', gate_id: 'gate-1' } },
      body: { expiry_minutes: 15 },
    })

    expect(wrapper.find('[data-testid="run-detail-approve"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-reject"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-hitl-notes"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a claim failure message', async () => {
    mockPendingGates = [gate()]
    mockClaimError = { detail: 'gate_already_claimed' }
    const wrapper = await mountAwaiting()

    await wrapper.find('[data-testid="run-detail-claim-gate"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Claim failed:')
    expect(wrapper.text()).toContain('gate_already_claimed')
    // still claimable
    expect(wrapper.find('[data-testid="run-detail-claim-gate"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows the claimed-by banner when another reviewer holds the gate', async () => {
    mockPendingGates = [gate({ claimed_by: 'ops@team' })]
    const wrapper = await mountAwaiting()
    expect(wrapper.text()).toContain('Claimed by ops@team')
    expect(wrapper.find('[data-testid="run-detail-claim-gate"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('approves the gate and resumes the run', async () => {
    mockPendingGates = [gate()]
    mockClaimResult = { claim_token: 'ct-123' }
    const { api } = await import('../lib/api/client')
    const wrapper = await mountAwaiting()
    await wrapper.find('[data-testid="run-detail-claim-gate"]').trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="run-detail-hitl-notes"]').setValue('looks good')
    await wrapper.find('[data-testid="run-detail-approve"]').trigger('click')
    await flushPromises()
    await nextTick()

    const post = (api.POST as any).mock.calls.find((c: unknown[]) => c[0] === '/api/v1/runs/{run_id}/hitl/{gate_id}/approve')
    expect((post as unknown[])[1]).toEqual({
      params: { path: { run_id: 'test-run-id', gate_id: 'gate-1' } },
      body: { claim_token: 'ct-123', notes: 'looks good' },
    })
    // the run flips to running and the gate section goes away
    expect(wrapper.text()).toContain('running')
    expect(wrapper.text()).not.toContain('HITL Gate')
    // BUG characterisation: the approve success message ("Gate approved.
    // Pipeline resuming.") is set inside approveGate() but rendered inside the
    // per-gate v-for, which is emptied on success — the reviewer never sees
    // positive feedback; the run-status flip is the only signal.
    expect(wrapper.text()).not.toContain('Gate approved. Pipeline resuming.')
    wrapper.unmount()
  })

  it('shows an approve failure message and keeps the actions', async () => {
    mockPendingGates = [gate()]
    mockClaimResult = { claim_token: 'ct-123' }
    mockApproveError = { detail: 'claim_expired' }
    const wrapper = await mountAwaiting()
    await wrapper.find('[data-testid="run-detail-claim-gate"]').trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="run-detail-approve"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Approve failed:')
    expect(wrapper.text()).toContain('claim_expired')
    expect(wrapper.find('[data-testid="run-detail-approve"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('rejects the gate and routes to the reject target', async () => {
    mockPendingGates = [gate()]
    mockClaimResult = { claim_token: 'ct-123' }
    const { api } = await import('../lib/api/client')
    const wrapper = await mountAwaiting()
    await wrapper.find('[data-testid="run-detail-claim-gate"]').trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="run-detail-reject"]').trigger('click')
    await flushPromises()
    await nextTick()

    const post = (api.POST as any).mock.calls.find((c: unknown[]) => c[0] === '/api/v1/runs/{run_id}/hitl/{gate_id}/reject')
    expect((post as unknown[])[1]).toEqual({
      params: { path: { run_id: 'test-run-id', gate_id: 'gate-1' } },
      body: { claim_token: 'ct-123', reason: 'Rejected by reviewer' },
    })
    // Same BUG characterisation as the approve flow: the reject success
    // message is rendered inside the emptied per-gate loop and is never seen.
    expect(wrapper.text()).not.toContain('Gate rejected. Pipeline routed to reject target.')
    expect(wrapper.text()).not.toContain('HITL Gate')
    wrapper.unmount()
  })

  it('shows a reject failure message', async () => {
    mockPendingGates = [gate()]
    mockClaimResult = { claim_token: 'ct-123' }
    mockRejectError = { detail: 'reject_target_missing' }
    const wrapper = await mountAwaiting()
    await wrapper.find('[data-testid="run-detail-claim-gate"]').trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="run-detail-reject"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Reject failed:')
    expect(wrapper.text()).toContain('reject_target_missing')
    wrapper.unmount()
  })
})

describe('RunDetailView guardrail override access', () => {
  async function mountBlocked() {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            ...baseDetail(),
            status: 'eval_failed',
            error_code: 'eval_blocked',
            guardrail_summary: { evaluated: 4, passed: 3, violated: 1 },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('shows the operator-required note for a non-operator session', async () => {
    // getAccessToken returns a non-JWT token in this spec, so readJwtPayload
    // yields no org_role ? the override button is hidden.
    const wrapper = await mountBlocked()
    expect(wrapper.find('[data-testid="run-detail-guardrail-override-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-override-guardrail"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="run-detail-override-role-note"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders guardrail buckets with their class variants', async () => {
    const wrapper = await mountBlocked()
    const buckets = wrapper.findAll('[data-testid="run-detail-guardrail-bucket"]')
    expect(buckets.length).toBe(3)
    const passed = buckets.find((b) => b.text().includes('Passed'))
    expect(passed?.text()).toContain('3')
    wrapper.unmount()
  })
})

describe('RunDetailView rendering extras', () => {
  async function mountWith(data: Record<string, unknown>, io: Record<string, unknown>) {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data, error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: io, error: undefined })
      if (url === '/api/v1/runs/{run_id}/workspace-lease') {
        return Promise.resolve({ data: mockWorkspaceLease, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('renders the workspace lease section with status, sandbox, duration and error', async () => {
    mockWorkspaceLease = { status: 'failed', sandbox_id: 'sbx-123', duration_seconds: 5400, error_message: 'OOM killed' }
    const wrapper = await mountWith(baseDetail(), { outputs_json: null })
    const ws = wrapper.text()
    expect(ws).toContain('Workspace')
    expect(ws).toContain('failed')
    expect(ws).toContain('OOM killed')
    expect(ws).toContain('1h 30m')
    wrapper.unmount()
  })

  it('formats sub-minute and minute workspace durations', async () => {
    mockWorkspaceLease = { status: 'completed', duration_seconds: 45 }
    const wrapper = await mountWith(baseDetail(), { outputs_json: null })
    expect(wrapper.text()).toContain('45s')
    wrapper.unmount()

    mockWorkspaceLease = { status: 'running', duration_seconds: 125 }
    const wrapper2 = await mountWith(baseDetail(), { outputs_json: null })
    expect(wrapper2.text()).toContain('2m 5s')
    wrapper2.unmount()
  })

  it('copies the trace id to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith({ ...baseDetail(), trace_id: 'trace-abc-123' }, { outputs_json: null })
    const btn = wrapper.find('[data-testid="run-detail-copy-trace-id"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('trace-abc-123')
    expect(wrapper.find('[data-testid="run-detail-copy-trace-id"]').text()).toContain('Copied!')
    wrapper.unmount()
  })

  it('copies the final output for a complete run', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(baseDetail(), {
      outputs_json: { 'node-a': { output: { result: 'final answer' } } },
    })
    const btn = wrapper.find('[data-testid="run-detail-copy-output"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()
    expect(writeText.mock.calls[0][0]).toContain('final answer')
    wrapper.unmount()
  })

  it('renders the cost clamped banner and the basis line for breakdown rows', async () => {
    const wrapper = await mountWith(
      {
        ...baseDetail(),
        total_cost_usd: '2.000000',
        cost_breakdown: [
          {
            component: 'model_cost',
            display_name: 'Model cost',
            source: 'estimated',
            amount_usd: '1.500000',
            basis: { reported: '1.2', node_count: 3 },
          },
          {
            component: 'eval_cost',
            display_name: 'Eval cost',
            source: 'estimated',
            amount_usd: '0.000000',
            error: 'eval_pricing_unavailable',
          },
        ],
      },
      { outputs_json: null },
    )
    expect(wrapper.find('[data-testid="run-detail-cost-clamped"]').exists()).toBe(false)
    const rows = wrapper.findAll('tbody tr')
    const modelRow = rows.find((r) => r.text().includes('Model cost'))!
    expect(modelRow.text()).toContain('reported=1.2, node_count=3')
    const evalRow = rows.find((r) => r.text().includes('Eval cost'))!
    // zero-amount row with an error stays visible with the eval error badge
    expect(evalRow.exists()).toBe(true)
    expect(evalRow.text()).toContain('eval error')
    expect(evalRow.text()).toContain('—')
    wrapper.unmount()
  })

  it('renders the clamped banner when any entry is total_clamped', async () => {
    const wrapper = await mountWith(
      {
        ...baseDetail(),
        total_cost_usd: '9.000000',
        cost_breakdown: [{ component: 'model_cost', amount_usd: '9.000000', total_clamped: true }],
      },
      { outputs_json: null },
    )
    expect(wrapper.find('[data-testid="run-detail-cost-clamped"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a dash basis line when the entry carries no basis object', async () => {
    const wrapper = await mountWith(
      {
        ...baseDetail(),
        cost_breakdown: [{ component: 'model_cost', amount_usd: '1.000000', basis: null }],
      },
      { outputs_json: null },
    )
    const row = wrapper.findAll('tbody tr').find((r) => r.text().includes('model_cost'))!
    expect(row.text()).toContain('—')
    wrapper.unmount()
  })

  it('uses human node labels from node_labels when provided', async () => {
    const wrapper = await mountWith(baseDetail(), {
      outputs_json: { 'node-a': { output: 'ok' } },
      node_labels: { 'node-a': 'Analyser' },
    })
    expect(wrapper.text()).toContain('Analyser')
    wrapper.unmount()
  })

  it('notes truncated agent logs past the 20000 character display cap', async () => {
    const bigStdout = 'x'.repeat(20001)
    const wrapper = await mountWith(baseDetail(), {
      outputs_json: null,
      node_telemetry: { 'node-a': { status: 'complete', agent_stdout: bigStdout } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    const logRow = wrapper.find('[data-testid="run-detail-log-row"]')
    expect(logRow.text()).toContain('Log truncated')
    wrapper.unmount()
  })

  it('resolves a legacy artifacts envelope to the output key with the run-level input fallback', async () => {
    const wrapper = await mountWith(baseDetail(), {
      outputs_json: { 'node-a': { artifacts: ['a'], output: { answer: 'legacy' } } },
      input_payload: { q: 'hi' },
    })
    const ioRow = wrapper.find('[data-testid="run-detail-io-row"]')
    expect(ioRow.exists()).toBe(true)
    const viewers = ioRow.findAll('[data-testid="json-viewer"]')
    expect(viewers.length).toBe(2)
    expect(viewers[0].text()).toContain('hi')
    expect(viewers[1].text()).toContain('legacy')
    wrapper.unmount()
  })

  it('treats a pure scalar node return as the output with the run input as input', async () => {
    const wrapper = await mountWith(baseDetail(), {
      outputs_json: { 'node-a': 'plain string return' },
      input_payload: { q: 'hi' },
    })
    const ioRow = wrapper.find('[data-testid="run-detail-io-row"]')
    const viewers = ioRow.findAll('[data-testid="json-viewer"]')
    expect(viewers.length).toBe(2)
    expect(viewers[0].text()).toContain('hi')
    expect(viewers[1].text()).toContain('plain string return')
    wrapper.unmount()
  })

  it('abbreviates token totals with k and M suffixes on the live cost line', async () => {
    const wrapper = await mountWith(
      {
        ...baseDetail(),
        status: 'running',
        node_token_usage: { 'node-a': { input_tokens: 700, output_tokens: 800, total_tokens: 1500 } },
      },
      { outputs_json: null },
    )
    const line = wrapper.find('[data-testid="run-detail-live-cost"]')
    expect(line.exists()).toBe(true)
    expect(line.text()).toContain('1.5k')
    wrapper.unmount()

    const wrapper2 = await mountWith(
      {
        ...baseDetail(),
        status: 'running',
        node_token_usage: { 'node-a': { input_tokens: 1000000, output_tokens: 1000000, total_tokens: 2000000 } },
      },
      { outputs_json: null },
    )
    expect(wrapper2.find('[data-testid="run-detail-live-cost"]').text()).toContain('2M')
    wrapper2.unmount()
  })

  it('renders created/started/completed timestamps and a dash for invalid dates', async () => {
    const wrapper = await mountWith(
      {
        ...baseDetail(),
        created_at: '2026-08-01T10:00:00Z',
        started_at: '2026-08-01T10:00:05Z',
        completed_at: '2026-08-01T10:05:00Z',
      },
      { outputs_json: null },
    )
    const tsRow = wrapper.text()
    expect(tsRow).toContain('Created')
    expect(tsRow).toContain('Started')
    expect(tsRow).toContain('Completed')
    wrapper.unmount()

    const wrapper2 = await mountWith(
      { ...baseDetail(), created_at: 'not-a-date' },
      { outputs_json: null },
    )
    expect(wrapper2.text()).toContain('—')
    wrapper2.unmount()
  })
})
