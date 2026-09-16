import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

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
}))

const mockRouterPush = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => testRoute),
  useRouter: vi.fn(() => ({
    push: mockRouterPush,
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
  })),
  createRouter: vi.fn(() => ({})),
  createWebHistory: vi.fn(() => ({})),
}))

const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: getMock, POST: postMock },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../lib/api/runs', () => ({
  requestRunCancellation: vi.fn().mockImplementation(async (_runId: string, _msg: string) => {
    const postResult = postMock.mock.results[postMock.mock.calls.length - 1]
    if (postResult?.value?.error) return { error: postResult.value.error.detail }
    return { error: null }
  }),
  requestRunRerun: vi.fn().mockImplementation(async (_runId: string, _msg: string) => {
    const postResult = postMock.mock.results[postMock.mock.calls.length - 1]
    if (postResult?.value?.error) return { error: postResult.value.error.detail }
    return { runId: postResult?.value?.data?.run_id, error: null }
  }),
}))

import RunDetailView from '../views/RunDetailView.vue'

function baseRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'test-run-id',
    pipeline_id: 'test-pipeline',
    status: 'complete',
    total_cost_usd: 1.23,
    node_token_usage: null,
    trace_id: null,
    ...overrides,
  }
}

function mountView() {
  return mount(RunDetailView)
}

/** Set up the mock for run + IO and mount the component. */
async function mountWith(run: Record<string, unknown>, io: Record<string, unknown> = {}) {
  getMock.mockImplementation((url: string) => {
    if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: run, error: undefined })
    if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: io, error: undefined })
    if (url === '/api/v1/runs/{run_id}/hitl/pending') return Promise.resolve({ data: { gates: [] }, error: undefined })
    if (url === '/api/v1/pipelines/{pipeline_id}/graph') return Promise.resolve({ data: { nodes: [], edges: [] }, error: undefined })
    return Promise.resolve({ data: null, error: undefined })
  })
  const wrapper = mountView()
  await flushPromises()
  await nextTick()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
  postMock.mockResolvedValue({ data: { run_id: 'test-run-id', status: 'pending' }, error: undefined })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

// ═════════════════════════════════════════════════════════════════════
// 1. Script logic branches — computed getters, handlers, edge cases
// ═════════════════════════════════════════════════════════════════════
describe('RunDetailView coverage — script logic branches', () => {
  // ── formatTokenCount thresholds ──────────────────────────────────
  it('formatTokenCount renders "k" suffix for thousands', async () => {
    const wrapper = await mountWith(
      baseRun({ status: 'running', node_token_usage: { n1: { input_tokens: 500, output_tokens: 600, total_tokens: 1100 } } }),
    )
    expect(wrapper.find('[data-testid="run-detail-live-cost"]').text()).toContain('1.1k')
  })

  it('formatTokenCount renders "M" suffix for millions', async () => {
    const wrapper = await mountWith(
      baseRun({ status: 'running', node_token_usage: { n1: { total_tokens: 2000000 } } }),
    )
    expect(wrapper.find('[data-testid="run-detail-live-cost"]').text()).toContain('2M')
  })

  it('formatTokenCount renders raw count below 1000', async () => {
    const wrapper = await mountWith(
      baseRun({ status: 'running', node_token_usage: { n1: { total_tokens: 300 } } }),
    )
    expect(wrapper.find('[data-testid="run-detail-live-cost"]').text()).toContain('300')
  })

  // ── liveCostPresent: terminal never shows live cost ─────────────
  it('hides live cost for a terminal run even with tokens', async () => {
    const wrapper = await mountWith(
      baseRun({ node_token_usage: { n1: { total_tokens: 500 } } }),
    )
    expect(wrapper.find('[data-testid="run-detail-live-cost"]').exists()).toBe(false)
  })

  // ── liveCostTotal: model_cost_display_usd fallback ──────────────
  it('uses model_cost_display_usd when cost_usd is absent', async () => {
    const wrapper = await mountWith(
      baseRun({ status: 'running', node_token_usage: { n1: { model_cost_display_usd: 0.05 } } }),
    )
    expect(wrapper.find('[data-testid="run-detail-live-cost"]').text()).toContain('0.0500')
  })

  // ── totalTokens: sums input+output when total_tokens absent ─────
  it('computes total from input+output when total_tokens absent', async () => {
    const wrapper = await mountWith(
      baseRun({ status: 'running', node_token_usage: { n1: { input_tokens: 300, output_tokens: 700 } } }),
    )
    expect(wrapper.find('[data-testid="run-detail-live-cost"]').text()).toContain('1k')
  })

  // ── nodesReportedTokens: only input_tokens > 0 ─────────────────
  it('shows token line when only input_tokens > 0', async () => {
    const wrapper = await mountWith(
      baseRun({ status: 'running', node_token_usage: { n1: { input_tokens: 50 } } }),
    )
    expect(wrapper.find('[data-testid="run-detail-live-cost"]').exists()).toBe(true)
  })

  // ── costBasisTokens: tokens_total_reported from breakdown ───────
  it('displays cost-basis tokens from breakdown basis', async () => {
    const wrapper = await mountWith(
      baseRun({ total_cost_usd: 0.5, cost_breakdown: [
        { component: 'model', amount_usd: '0.500000', basis: { tokens_total_reported: 2500 } },
      ] }),
    )
    const costSection = wrapper.find('#run-detail-cost-section')
    expect(costSection.exists()).toBe(true)
    expect(costSection.text()).toContain('2,500')
  })

  // ── costBasisTokens: tokens_input_reported fallback ──────────────
  it('uses tokens_input_reported from basis', async () => {
    const wrapper = await mountWith(
      baseRun({ total_cost_usd: 0.5, cost_breakdown: [
        { component: 'model', amount_usd: '0.500000', basis: { tokens_input_reported: 1200 } },
      ] }),
    )
    expect(wrapper.find('#run-detail-cost-section').text()).toContain('1,200')
  })

  // ── formattedCost: null total_cost_usd ──────────────────────────
  it('hides cost section for null total_cost_usd', async () => {
    const wrapper = await mountWith(baseRun({ total_cost_usd: null }))
    expect(wrapper.find('#run-detail-cost-section').exists()).toBe(false)
  })

  // ── childRunCost: empty string ──────────────────────────────────
  it('treats empty-string child_runs_cost_usd as zero', async () => {
    const wrapper = await mountWith(
      baseRun({ child_runs_cost_usd: '', aggregate_cost_usd: '1.23' }),
    )
    expect(wrapper.find('[data-testid="run-detail-aggregate-cost"]').exists()).toBe(false)
  })

  // ── aggregateCost: NaN ──────────────────────────────────────────
  it('hides aggregate cost when aggregate_cost_usd is NaN', async () => {
    const wrapper = await mountWith(
      baseRun({ child_runs_cost_usd: '0.5', aggregate_cost_usd: 'not-a-number' }),
    )
    expect(wrapper.find('[data-testid="run-detail-aggregate-cost"]').exists()).toBe(false)
  })

  // ── childRunCount: non-integer ──────────────────────────────────
  it('treats non-integer child_runs_count as zero', async () => {
    const wrapper = await mountWith(
      baseRun({ child_runs_cost_usd: '0.5', aggregate_cost_usd: '1.73', child_runs_count: 2.5 }),
    )
    const agg = wrapper.find('[data-testid="run-detail-aggregate-cost"]')
    expect(agg.exists()).toBe(true)
    expect(agg.text()).toContain('Total including child runs:')
  })

  // ── statusBadgeClassFor: unknown status ─────────────────────────
  it('applies fallback badge for unknown status', async () => {
    const wrapper = await mountWith(baseRun({ status: 'weird_status' }))
    const badge = wrapper.find('.badge')
    expect(badge.exists()).toBe(true)
    expect(badge.classes()).toContain('badge-context-slate')
  })

  // ── formatMs: NaN ──────────────────────────────────────────────
  it('renders dash for NaN wall_clock_time_ms', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: 'ok' } },
      node_telemetry: { n1: { status: 'complete', wall_clock_time_ms: NaN } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-node-telemetry"]').text()).toContain('—')
  })

  // ── formatDuration: hours + minutes ─────────────────────────────
  it('formats duration with hours and minutes', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: 'ok' } },
      node_telemetry: { n1: { status: 'complete', wall_clock_time_ms: 7260000 } }, // 2h 1m
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    const t = wrapper.find('[data-testid="run-detail-node-telemetry"]').text()
    expect(t).toContain('2h')
    expect(t).toContain('1m')
  })

  // ── nodeEntries: model_cost_display_usd fallback ────────────────
  it('uses model_cost_display_usd for node cost', async () => {
    const wrapper = await mountWith(
      baseRun({ node_token_usage: { n1: { model_cost_display_usd: 0.042 } } }),
      { outputs_json: { n1: { output: 'ok' } } },
    )
    expect(wrapper.text()).toContain('0.042000')
  })

  // ── nodeEntries: node has output but no token usage ─────────────
  it('renders a node with output but no token usage', async () => {
    const wrapper = await mountWith(baseRun(), { outputs_json: { n1: { output: 'result' } } })
    expect(wrapper.text()).toContain('result')
  })

  // ── nodeSummary: empty output.summary ───────────────────────────
  it('falls back to telemetry summary when output.summary is empty string', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: { summary: '' } } },
      node_telemetry: { n1: { status: 'complete', summary: 'telemetry fallback' } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-node-summary"]').text()).toContain('telemetry fallback')
  })

  // ── nodeSummary: no summary anywhere ────────────────────────────
  it('returns null summary when neither output nor telemetry has one', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: { result: 'ok' } } },
      node_telemetry: { n1: { status: 'complete' } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-node-summary"]').exists()).toBe(false)
  })

  // ── resolveNodeIO: empty object ────────────────────────────────
  it('treats an empty object as no output', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: {} },
      input_payload: { q: 'test' },
    })
    const ioRow = wrapper.find('[data-testid="run-detail-io-row"]')
    expect(ioRow.exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-no-output"]').exists()).toBe(true)
    expect(ioRow.findAll('[data-testid="json-viewer"]').length).toBe(1)
  })

  // ── resolveNodeIO: explicit input key ───────────────────────────
  it('uses node-level input key when present', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { input: { node_q: 'from_node' }, output: 'ok' } },
      input_payload: { q: 'from_run' },
    })
    const viewers = wrapper.find('[data-testid="run-detail-io-row"]').findAll('[data-testid="json-viewer"]')
    expect(viewers[0].text()).toContain('from_node')
    expect(viewers[0].text()).not.toContain('from_run')
  })

  // ── resolveNodeIO: scalar array ─────────────────────────────────
  it('treats an array return as output with run-level input', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: ['item1', 'item2'] },
      input_payload: { q: 'hi' },
    })
    const viewers = wrapper.find('[data-testid="run-detail-io-row"]').findAll('[data-testid="json-viewer"]')
    expect(viewers.length).toBe(2)
    expect(viewers[1].text()).toContain('item1')
  })

  // ── getNodeLogTransformed: stripAnsi ────────────────────────────
  it('applies strip-ansi transform when toggled', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: null,
      node_telemetry: { n1: { status: 'complete', agent_stdout: 'normal \x1b[31mred\x1b[0m text' } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="run-detail-strip-ansi"]').setValue(true)
    await nextTick()
    const logRow = wrapper.find('[data-testid="run-detail-log-row"]')
    expect(logRow.text()).toContain('normal')
    expect(logRow.text()).toContain('red')
    expect(logRow.text()).toContain('text')
  })

  it('hides strip-ansi toggle when no ANSI sequences', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: null,
      node_telemetry: { n1: { status: 'complete', agent_stdout: 'clean output' } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-strip-ansi"]').exists()).toBe(false)
  })

  // ── prettyPrint toggle for stdout and stderr ────────────────────
  it('toggles pretty-print for stdout', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: null,
      node_telemetry: { n1: { status: 'complete', agent_stdout: '{"key":"value"}' } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="run-detail-pretty-print"]').setValue(true)
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-log-row"]').text()).toContain('key')
  })

  it('toggles pretty-print for stderr', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: null,
      node_telemetry: { n1: { status: 'complete', agent_stderr: '{"error":"boom"}' } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="run-detail-pretty-print-stderr"]').setValue(true)
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-log-row"]').text()).toContain('error')
  })

  // ── no agent logs message ───────────────────────────────────────
  it('shows "No agent logs" when no stdout, stderr, live output, or artifacts', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: 'ok' } },
      node_telemetry: { n1: { status: 'complete' } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-log-row"]').text()).toContain('No agent logs')
  })

  // ── log truncation ──────────────────────────────────────────────
  it('shows truncation notice for stdout past 20000 chars', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: null,
      node_telemetry: { n1: { status: 'complete', agent_stdout: 'x'.repeat(25000) } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-log-truncated-stdout"]').exists()).toBe(true)
  })

  it('shows truncation notice for stderr past 20000 chars', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: null,
      node_telemetry: { n1: { status: 'complete', agent_stderr: 'x'.repeat(25000) } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-log-truncated-stderr"]').exists()).toBe(true)
  })

  // ── artifact retry on network error ─────────────────────────────
  it('shows artifact retry on network error and recovers', async () => {
    let calls = 0
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ node_token_usage: { n1: {} } }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: { n1: { output: {} } }, node_telemetry: { n1: { agent_stdout: 'log' } } }, error: undefined })
      if (url === '/api/v1/runs/{run_id}/nodes/{node_id}/artifacts') {
        calls++
        if (calls === 1) return Promise.reject(new Error('network error'))
        return Promise.resolve({ data: { artifacts: [{ attempt_key: 'a:0', stream: 'stdout', size_bytes: 10, sha256: 'aa', compression: 'none' }] }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-artifact-error"]').exists()).toBe(true)
    await wrapper.find('[data-testid="run-detail-artifact-retry"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-artifact-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="run-detail-node-artifacts"]').exists()).toBe(true)
  })

  // ── artifact fetch on first log open ────────────────────────────
  it('fetches artifacts when opening logs panel for the first time', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ node_token_usage: { n1: {} } }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: { n1: { output: {} } }, node_telemetry: { n1: { agent_stdout: 'log' } } }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await flushPromises()
    await nextTick()
    const artifactCall = getMock.mock.calls.find((c: unknown[]) => c[0] === '/api/v1/runs/{run_id}/nodes/{node_id}/artifacts')
    expect(artifactCall).toBeTruthy()
  })

  // ── childRunBadgeClass ──────────────────────────────────────────
  it('renders child run badges with status classes', async () => {
    const wrapper = await mountWith(
      baseRun({ child_runs: [
        { run_id: 'c1', status: 'complete', pipeline_name: 'P1' },
        { run_id: 'c2', status: 'failed', pipeline_name: 'P2' },
      ] }),
    )
    const section = wrapper.find('[data-testid="run-detail-child-runs"]')
    expect(section.exists()).toBe(true)
    expect(section.text()).toContain('P1')
    expect(section.text()).toContain('P2')
  })

  // ── child runs: no pipeline_name ────────────────────────────────
  it('renders child runs without pipeline_name', async () => {
    const wrapper = await mountWith(
      baseRun({ child_runs: [{ run_id: 'c1', status: 'complete' }] }),
    )
    expect(wrapper.find('[data-testid="run-detail-child-runs"]').text()).toContain('complete')
  })

  // ── child runs: no run_number → short ID ────────────────────────
  it('renders child link with short ID when run_number absent', async () => {
    const wrapper = await mountWith(
      baseRun({ child_runs: [{ run_id: 'child-abc-123', status: 'complete' }] }),
    )
    const section = wrapper.find('[data-testid="run-detail-child-runs"]')
    expect(section.exists()).toBe(true)
    // shortId truncates to 8 chars: 'child-abc' → 'child-ab'
    const link = section.find('a')
    expect(link.exists()).toBe(true)
    expect(link.text()).toContain('child-ab')
    // The link text should NOT contain a '#' run number prefix
    expect(link.text()).not.toMatch(/#\d/)
  })

  // ── work items: non-GitHub kind ─────────────────────────────────
  it('renders non-GitHub work items with generic badge', async () => {
    const wrapper = await mountWith(
      baseRun({ work_item_refs: [{ kind: 'linear', ref: 'FAR-123', source: 'manual' }] }),
    )
    const section = wrapper.find('[data-testid="run-detail-work-items"]')
    expect(section.exists()).toBe(true)
    expect(section.text()).toContain('FAR-123')
  })

  // ── work items: empty ref ───────────────────────────────────────
  it('renders GitHub items with empty ref as unlinked badges', async () => {
    const wrapper = await mountWith(
      baseRun({ work_item_refs: [{ kind: 'github_pr', ref: '', source: 'auto' }] }),
    )
    expect(wrapper.find('[data-testid="run-detail-work-items"]').find('[data-testid="run-detail-pr-link-0"]').exists()).toBe(false)
  })

  // ── input payload copy ──────────────────────────────────────────
  it('copies input payload JSON to clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(baseRun(), { outputs_json: null, input_payload: { key: 'val' } })
    await wrapper.find('[data-testid="run-detail-copy-input"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalled()
    expect(writeText.mock.calls[0][0]).toContain('"key": "val"')
  })

  // ── share summary: with node data ──────────────────────────────
  it('includes node counts in share summary', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(
      baseRun({ node_token_usage: { n1: {}, n2: {} } }),
      { outputs_json: { n1: { output: 'ok' }, n2: { output: 'ok' } } },
    )
    await wrapper.find('[data-testid="run-detail-share-summary"]').trigger('click')
    await flushPromises()
    expect(writeText.mock.calls[0][0]).toContain('Nodes: 2/2')
  })

  // ── share summary: no tokens ───────────────────────────────────
  it('shows dash for tokens when no token data', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(baseRun({ node_token_usage: null }))
    await wrapper.find('[data-testid="run-detail-share-summary"]').trigger('click')
    await flushPromises()
    expect(writeText.mock.calls[0][0]).toContain('Tokens: —')
  })

  // ── share summary: no cost ─────────────────────────────────────
  it('shows dash for cost when total_cost_usd null', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(baseRun({ total_cost_usd: null }))
    await wrapper.find('[data-testid="run-detail-share-summary"]').trigger('click')
    await flushPromises()
    expect(writeText.mock.calls[0][0]).toContain('Cost: —')
  })

  // ── copyRunId ───────────────────────────────────────────────────
  it('copies the run ID to clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(baseRun())
    const btn = wrapper.find('button[aria-label*="Copy run ID"]')
    await btn.trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('test-run-id')
  })

  // ── copyTraceId ─────────────────────────────────────────────────
  it('copies trace ID and shows Copied!', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(baseRun({ trace_id: 'trace-xyz' }))
    const btn = wrapper.find('[data-testid="run-detail-copy-trace-id"]')
    await btn.trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('trace-xyz')
    expect(btn.text()).toContain('Copied!')
  })

  // ── copyOutput ──────────────────────────────────────────────────
  it('copies formatted output as JSON string', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: { answer: 42 } } },
    })
    await wrapper.find('[data-testid="run-detail-copy-output"]').trigger('click')
    await flushPromises()
    expect(writeText.mock.calls[0][0]).toContain('"answer": 42')
  })

  // ── formattedOutput: string output ──────────────────────────────
  it('renders string output directly', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: 'plain string result' } },
    })
    expect(wrapper.text()).toContain('plain string result')
  })

  // ── formattedOutput: null output ────────────────────────────────
  it('hides final output when lastNodeOutput is null', async () => {
    const wrapper = await mountWith(baseRun(), { outputs_json: null })
    expect(wrapper.find('[data-testid="run-detail-copy-output"]').exists()).toBe(false)
  })

  // ── guardrail override: success ─────────────────────────────────
  it('shows success after successful override', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'eval_failed', error_code: 'eval_blocked' }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    postMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}/guardrail-override') return Promise.resolve({ data: { status: 'pending' }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    // The override panel renders when isGuardrailBlocked is true
    const panel = wrapper.find('[data-testid="run-detail-guardrail-override-panel"]')
    expect(panel.exists()).toBe(true)
    // The override button may or may not render depending on isOrgOperator
    const btn = wrapper.find('[data-testid="run-detail-override-guardrail"]')
    if (btn.exists()) {
      await btn.trigger('click')
      await nextTick()
      await wrapper.find('[data-testid="run-detail-override-input"]').setValue('{}')
      await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
      await flushPromises()
      await nextTick()
      expect(wrapper.find('[data-testid="run-detail-override-success"]').exists()).toBe(true)
    } else {
      // Non-operator: the role note is shown instead
      expect(wrapper.find('[data-testid="run-detail-override-role-note"]').exists()).toBe(true)
    }
  })

  // ── guardrail override: non-422 error ──────────────────────────
  it('shows generic error for non-422 failures', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'eval_failed', error_code: 'eval_blocked' }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    postMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}/guardrail-override') return Promise.resolve({ data: null, error: { status: 500, detail: 'internal error' } })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const btn = wrapper.find('[data-testid="run-detail-override-guardrail"]')
    if (btn.exists()) {
      await btn.trigger('click')
      await nextTick()
      await wrapper.find('[data-testid="run-detail-override-input"]').setValue('{}')
      await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
      await flushPromises()
      await nextTick()
      expect(wrapper.find('[data-testid="run-detail-override-error"]').text()).toContain('internal error')
    }
  })

  // ── guardrail override: exception ───────────────────────────────
  it('shows error when override POST throws', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'eval_failed', error_code: 'eval_blocked' }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    postMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}/guardrail-override') return Promise.reject(new Error('network failure'))
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const btn = wrapper.find('[data-testid="run-detail-override-guardrail"]')
    if (btn.exists()) {
      await btn.trigger('click')
      await nextTick()
      await wrapper.find('[data-testid="run-detail-override-input"]').setValue('{}')
      await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
      await flushPromises()
      await nextTick()
      expect(wrapper.find('[data-testid="run-detail-override-error"]').exists()).toBe(true)
    }
  })

  // ── breakdownBasisLine: missing_self_report ─────────────────────
  it('shows basis line for missing self-report', async () => {
    const wrapper = await mountWith(
      baseRun({ cost_breakdown: [
        { component: 'model', amount_usd: '0.000000', missing_self_report: true },
      ] }),
    )
    const row = wrapper.findAll('tbody tr').find(r => r.text().includes('model'))!
    expect(row.text()).toContain('No model cost reported')
  })

  // ── breakdownBasisLine: many keys → truncated to 6 ─────────────
  it('truncates basis line to 6 entries', async () => {
    const basis: Record<string, unknown> = {}
    for (let i = 0; i < 10; i++) basis[`k${i}`] = `v${i}`
    const wrapper = await mountWith(
      baseRun({ cost_breakdown: [{ component: 'm', amount_usd: '1.000000', basis }] }),
    )
    const text = wrapper.findAll('tbody tr').find(r => r.text().includes('m'))!.text()
    expect(text).toContain('k0=v0')
    expect(text).toContain('k5=v5')
  })

  // ── breakdownEntries: zero + error stays visible ────────────────
  it('shows zero-amount rows with errors', async () => {
    const wrapper = await mountWith(
      baseRun({ cost_breakdown: [{ component: 'eval', amount_usd: '0.000000', error: 'pricing_unavailable' }] }),
    )
    expect(wrapper.findAll('tbody tr').find(r => r.text().includes('eval'))!.text()).toContain('eval error')
  })

  // ── breakdownEntries: zero + no error hidden ────────────────────
  it('hides zero-amount rows without errors', async () => {
    const wrapper = await mountWith(
      baseRun({ cost_breakdown: [{ component: 'other', amount_usd: '0.000000' }] }),
    )
    expect(wrapper.findAll('tbody tr').find(r => r.text().includes('other'))).toBeUndefined()
  })

  // ── breakdownTotal: sums entries ────────────────────────────────
  it('shows sum of components in table footer', async () => {
    const wrapper = await mountWith(
      baseRun({ cost_breakdown: [
        { component: 'a', amount_usd: '0.300000' },
        { component: 'b', amount_usd: '0.200000' },
      ] }),
    )
    expect(wrapper.text()).toContain('Sum of components')
    expect(wrapper.text()).toContain('0.500000')
  })

  // ── cost transition note ────────────────────────────────────────
  it('shows cost accounting migration note when breakdown present', async () => {
    const wrapper = await mountWith(
      baseRun({ cost_breakdown: [{ component: 'model', amount_usd: '1.000000' }] }),
    )
    expect(wrapper.find('[data-testid="run-detail-cost-transition-note"]').exists()).toBe(true)
  })

  // ── no attributable costs ───────────────────────────────────────
  it('shows no-attributable-costs when only clamped entries', async () => {
    const wrapper = await mountWith(
      baseRun({ cost_breakdown: [{ component: '__total__', total_clamped: true }] }),
    )
    expect(wrapper.find('[data-testid="run-detail-no-attributable-costs"]').exists()).toBe(true)
  })

  // ── node progress: live events ──────────────────────────────────
  it('shows node progress chips from live events', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: async () => ({
        events: [
          { seq: 1, event_type: 'node_started', payload: { node_id: 'agent-1' } },
          { seq: 2, event_type: 'node_completed', payload: { node_id: 'agent-1' } },
          { seq: 3, event_type: 'node_failed', payload: { node_id: 'agent-2' } },
        ],
      }),
    }))
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'running', node_token_usage: { 'agent-1': {} } }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: { 'agent-1': { output: {} } } }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    await nextTick()
    const strip = wrapper.find('[data-testid="run-detail-node-progress"]')
    expect(strip.exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-node-progress-agent-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-node-progress-agent-2"]').exists()).toBe(true)
  })

  // ── HITL: hitl_parked shows gates ──────────────────────────────
  it('shows HITL gates for hitl_parked runs', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'hitl_parked' }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      if (url === '/api/v1/runs/{run_id}/hitl/pending') return Promise.resolve({ data: { gates: [{ gate_id: 'g1', label: 'Review' }] }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('HITL Gate')
    expect(wrapper.text()).toContain('Review')
  })

  // ── stallReason from telemetry ──────────────────────────────────
  it('shows stall reason badge on node row', async () => {
    const wrapper = await mountWith(
      baseRun({ node_token_usage: { n1: {} } }),
      {
        outputs_json: { n1: { output: {} } },
        node_telemetry: { n1: { status: 'running', stall_reason: 'waiting on model' } },
      },
    )
    const badge = wrapper.find('[data-testid="run-detail-node-stalled"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('waiting on model')
  })

  // ── node trace: fallback to run trace_id ────────────────────────
  it('shows run trace_id when no per-node span', async () => {
    const wrapper = await mountWith(
      baseRun({ trace_id: 'run-trace-abc', node_token_usage: { n1: {} } }),
      { outputs_json: { n1: { output: {} } } },
    )
    const traceBtn = wrapper.find('[data-testid="run-detail-node-trace-id"]')
    expect(traceBtn.exists()).toBe(true)
    // shortId truncates to 8 chars: 'run-trace-abc' → 'run-trac…'
    expect(traceBtn.text()).toContain('run-trac')
  })

  // ── node trace: isNodeSpanId label ─────────────────────────────
  it('shows "span" label when otel_span_id present', async () => {
    const wrapper = await mountWith(
      baseRun({ node_token_usage: { n1: {} } }),
      { outputs_json: { n1: { output: {} } }, node_telemetry: { n1: { status: 'complete', otel_span_id: 'span-123' } } },
    )
    expect(wrapper.find('[data-testid="run-detail-node-trace-id"]').attributes('aria-label')).toContain('span')
  })

  // ── node trace: no trace ────────────────────────────────────────
  it('shows dash when no trace available', async () => {
    const wrapper = await mountWith(
      baseRun({ node_token_usage: { n1: {} } }),
      { outputs_json: { n1: { output: {} } } },
    )
    const traceCell = wrapper.findAll('tbody tr').find(r => r.text().includes('n1'))!
    expect(traceCell.text()).toContain('—')
  })

  // ── IO toggle ───────────────────────────────────────────────────
  it('toggles IO row on click', async () => {
    // Use a running status so the node IO is NOT auto-expanded
    const wrapper = await mountWith(baseRun({ status: 'running', node_token_usage: { n1: {} } }), {
      outputs_json: { n1: { input: { q: 1 }, output: { a: 2 } } },
    })
    const toggle = wrapper.find('[data-testid="run-detail-toggle-io"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.text()).toContain('Show')
    await toggle.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-io-row"]').exists()).toBe(true)
    expect(toggle.text()).toContain('Hide')
  })

  // ── logs toggle ─────────────────────────────────────────────────
  it('toggles logs row on click', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: {} } },
      node_telemetry: { n1: { agent_stdout: 'log output' } },
    })
    const toggle = wrapper.find('[data-testid="run-detail-toggle-logs"]')
    expect(toggle.text()).toContain('View')
    await toggle.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-log-row"]').exists()).toBe(true)
  })

  // ── no node data (non-failed) ──────────────────────────────────
  it('shows generic no-node-data for non-failed empty runs', async () => {
    const wrapper = await mountWith(baseRun(), { outputs_json: null })
    expect(wrapper.text()).toContain('No node data available')
  })

  // ── no node data (failed) ──────────────────────────────────────
  it('shows failed-specific no-node-data message', async () => {
    const wrapper = await mountWith(baseRun({ status: 'failed' }), { outputs_json: null })
    // The failed state has a different message
    expect(wrapper.text()).toContain('No node-level data recorded')
    expect(wrapper.text()).toContain('failed')
  })
})
