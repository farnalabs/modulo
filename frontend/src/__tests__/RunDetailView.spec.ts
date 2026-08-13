import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

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
              status: 'complete',
              total_cost_usd: 1.23,
              token_consumption: null,
              node_token_usage: { 'node-a': { input_tokens: 10, output_tokens: 20, total_tokens: 30 } },
              trace_id: null
            },
            error: undefined
          })
        }
        if (url === '/api/v1/runs/{run_id}/io') {
          return Promise.resolve({
            data: {
              outputs_json: { 'node-a': { input: { q: 'hello' }, output: 'response' } },
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
            error_code: 'org_capacity_limited',
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
            error_code: 'task_failure',
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
    expect(wrapper.text()).toContain('task_failure')
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
})
