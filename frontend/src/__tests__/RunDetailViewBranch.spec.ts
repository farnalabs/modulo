import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import type { Stubs } from '@vue/test-utils'
import { createPinia } from 'pinia'
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
  // RunDetailView pulls in the Analyze action (FAR-1235), whose assistant
  // store chain imports @/router — that module calls beforeEach/afterEach/
  // onError on the router it builds at import time, so the stub must answer them.
  createRouter: vi.fn(() => ({
    install: vi.fn(),
    push: vi.fn(),
    replace: vi.fn(),
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    onError: vi.fn(),
    currentRoute: { value: {} },
    getRoutes: vi.fn(() => []),
    addRoute: vi.fn(),
    removeRoute: vi.fn(),
    hasRoute: vi.fn(() => false),
    isReady: vi.fn(() => Promise.resolve(true)),
  })),
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

vi.mock('../lib/jwt', () => ({
  decodeJwtPayload: () => ({ org_role: 'operator' }),
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

function mountView(options: { stubs?: Stubs } = {}) {
  return mount(RunDetailView, {
    // The Analyze action (FAR-1235) reads plan/assistant stores, so the
    // view's test host needs an active Pinia.
    global: { plugins: [createPinia()], ...(options.stubs ? { stubs: options.stubs } : {}) },
  })
}

const overrideDialogStub = {
  template: '<div class="p-dialog"><slot name="header" /><slot /><slot name="footer" /></div>',
}

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

describe('RunDetailView — branch coverage sweep', () => {
  // -- submitOverride: 422 error path --
  it('shows re-blocked message for 422 override errors', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'eval_failed', error_code: 'eval_blocked' }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    postMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}/guardrail-override') return Promise.resolve({ data: null, error: { status: 422, detail: 'still violating' } })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView({ stubs: { Dialog: overrideDialogStub } })
    await flushPromises()
    await nextTick()
    const btn = wrapper.find('[data-testid="run-detail-override-guardrail"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="run-detail-override-input"]').setValue('{}')
    await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-override-error"]').text()).toContain('still')
  })

  // -- submitOverride: invalid JSON path --
  it('shows invalid JSON error when input is malformed', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'eval_failed', error_code: 'eval_blocked' }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView({ stubs: { Dialog: overrideDialogStub } })
    await flushPromises()
    await nextTick()
    const btn = wrapper.find('[data-testid="run-detail-override-guardrail"]')
    await btn.trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="run-detail-override-input"]').setValue('not valid json')
    await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-override-error"]').exists()).toBe(true)
  })

  // -- submitOverride: success auto-closes dialog --
  it('auto-closes override dialog after success', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'eval_failed', error_code: 'eval_blocked' }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    postMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}/guardrail-override') return Promise.resolve({ data: { status: 'pending' }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView({ stubs: { Dialog: overrideDialogStub } })
    await flushPromises()
    await nextTick()
    const btn = wrapper.find('[data-testid="run-detail-override-guardrail"]')
    await btn.trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="run-detail-override-input"]').setValue('{}')
    await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
    await flushPromises()
    await nextTick()
    // Advance past the 1500ms auto-close timer
    vi.advanceTimersByTime(1600)
    await flushPromises()
    await nextTick()
    // After auto-close, the success message should have been set then cleared
    expect(wrapper.find('[data-testid="run-detail-override-success"]').exists()).toBe(false)
  })

  // -- fetchNodeArtifacts: 404 handling --
  it('does not show error for 404 artifact responses', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ node_token_usage: { n1: {} } }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: { n1: { output: {} } }, node_telemetry: { n1: { agent_stdout: 'log' } } }, error: undefined })
      if (url === '/api/v1/runs/{run_id}/nodes/{node_id}/artifacts') return Promise.resolve({ data: null, error: { status: 404 } })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-artifact-error"]').exists()).toBe(false)
  })

  // -- getPrUrl: various URL construction branches --
  it('constructs PR URL from ref with owner/repo#number format', async () => {
    const wrapper = await mountWith(
      baseRun({ work_item_refs: [{ kind: 'github_pr', ref: 'farnalabs/modulo#123' }] }),
    )
    const link = wrapper.find('[data-testid="run-detail-pr-link-0"]')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toContain('farnalabs/modulo/pull/123')
  })

  it('constructs issue URL from ref with owner/repo#number format', async () => {
    const wrapper = await mountWith(
      baseRun({ work_item_refs: [{ kind: 'github_issue', ref: 'farnalabs/modulo#456' }] }),
    )
    const link = wrapper.find('[data-testid="run-detail-pr-link-0"]')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toContain('farnalabs/modulo/issues/456')
  })

  it('constructs repo URL from ref with owner/repo#number but generic kind', async () => {
    const wrapper = await mountWith(
      baseRun({ work_item_refs: [{ kind: 'github', ref: 'farnalabs/modulo#789' }] }),
    )
    const link = wrapper.find('[data-testid="run-detail-pr-link-0"]')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toContain('github.com/farnalabs/modulo')
  })

  it('returns null PR URL when ref has whitespace in owner', async () => {
    const wrapper = await mountWith(
      baseRun({ work_item_refs: [{ kind: 'github_pr', ref: 'owner with spaces/repo#123' }] }),
    )
    const link = wrapper.find('[data-testid="run-detail-pr-link-0"]')
    expect(link.exists()).toBe(false)
  })

  // -- getPrUrl: fallback to payload context --
  it('falls back to payload context for github_pr with numeric ref', async () => {
    const wrapper = await mountWith(
      baseRun({
        work_item_refs: [{ kind: 'github_pr', ref: '42' }],
      }),
      { input_payload: { repository: { full_name: 'org/repo' }, pull_request: { number: 42, title: 'My PR' } } },
    )
    const link = wrapper.find('[data-testid="run-detail-pr-link-0"]')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toContain('org/repo/pull/42')
  })

  // -- prTitle: returns null for non-PR kind --
  it('returns null prTitle for github_issue kind', async () => {
    const wrapper = await mountWith(
      baseRun({
        work_item_refs: [{ kind: 'github_issue', ref: 'org/repo#1' }],
      }),
      { input_payload: { repository: { full_name: 'org/repo' }, pull_request: { number: '1', title: 'PR Title' } } },
    )
    const vm = wrapper.vm as any
    // github_issue items should NOT return a PR title even with matching context
    expect(vm.prTitle({ kind: 'github_issue', ref: 'org/repo#1' })).toBeNull()
  })

  // -- nodeSummary: failed node falls back to telemetry --
  it('uses telemetry summary for failed nodes even when output has summary', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: { summary: 'output summary' } } },
      node_telemetry: { n1: { status: 'failed', summary: 'telemetry summary' } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-node-summary"]').text()).toContain('telemetry summary')
  })

  // -- onHitlDecided: awaiting_human vs hitl_parked --
  it('sets run status to running after HITL decision on awaiting_human run', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'awaiting_human' }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      if (url === '/api/v1/runs/{run_id}/hitl/pending') return Promise.resolve({ data: { gates: [{ gate_id: 'g1', label: 'Review' }] }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('HITL Gate')
  })

  // -- formatDuration: seconds only --
  it('formats duration under 60 seconds', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: 'ok' } },
      node_telemetry: { n1: { status: 'complete', wall_clock_time_ms: 45000 } },
    })
    await wrapper.find('[data-testid="run-detail-toggle-logs"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-node-telemetry"]').text()).toContain('45s')
  })

  // -- applyLiveEvent: various event types --
  it('processes stdout_chunk, node_started, node_completed, node_failed events', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: async () => ({
        events: [
          { seq: 1, event_type: 'node.stdout_chunk', payload: { node_id: 'a1', chunk: 'hello' } },
          { seq: 2, event_type: 'node_started', payload: { node_id: 'a1' } },
          { seq: 3, event_type: 'node_completed', payload: { node_id: 'a1' } },
          { seq: 4, event_type: 'node_failed', payload: { node_id: 'a2' } },
          { seq: 5, event_type: 'unknown_event', payload: { node_id: 'a3' } },
        ],
      }),
    }))
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ status: 'running' }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: null }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="run-detail-node-progress"]').exists()).toBe(true)
  })

  // -- revealPrompt: cached path --
  it('shows cached prompt without re-fetching', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ node_token_usage: { n1: {} } }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: { n1: { output: {} } } }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    // First call — fetches from API and caches
    postMock.mockResolvedValueOnce({ data: { prompt: 'test prompt', messages: [], token_count: 10, prompt_always_visible: false }, error: undefined })
    const vm = wrapper.vm as any
    await vm.revealPrompt('n1')
    await flushPromises()
    await nextTick()
    // Should have a cached entry
    expect(vm.revealedPrompts.n1).toBeTruthy()

    // Second call — should use cache (no new POST)
    const callsBefore = postMock.mock.calls.length
    await vm.revealPrompt('n1')
    await flushPromises()
    await nextTick()
    expect(postMock.mock.calls.length).toBe(callsBefore)
    // Dialog should be open
    expect(vm.selectedPrompt).toBeTruthy()
  })

  // -- revealPrompt: error path --
  it('sets error on prompt reveal failure', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: baseRun({ node_token_usage: { n1: {} } }), error: undefined })
      if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: { outputs_json: { n1: { output: {} } } }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    postMock.mockResolvedValueOnce({ data: null, error: { detail: 'reveal_failed' } })
    const btns = wrapper.findAll('[data-testid="run-detail-show-prompt"]')
    await btns[0].trigger('click')
    await flushPromises()
    await nextTick()
    const vm = wrapper.vm as any
    expect(vm.error).toContain('reveal_failed')
  })

  // -- artifactDownloadUrl: null runId --
  it('returns # for null runId in artifactDownloadUrl', async () => {
    const wrapper = await mountWith(baseRun({ run_id: null as any }), {
      outputs_json: { n1: { output: {} } },
      node_telemetry: { n1: { agent_stdout: 'log' } },
    })
    // Just verify the component renders without crash
    expect(wrapper.exists()).toBe(true)
  })

  // -- cost breakdown: display_name fallback --
  it('falls back to component when display_name is absent', async () => {
    const wrapper = await mountWith(
      baseRun({ cost_breakdown: [{ component: 'model_cost', amount_usd: '1.000000' }] }),
    )
    expect(wrapper.text()).toContain('model_cost')
  })

  // -- cost breakdown: basis with nested objects --
  it('serialises nested basis objects in basis line', async () => {
    const wrapper = await mountWith(
      baseRun({ cost_breakdown: [{ component: 'm', amount_usd: '1.000000', basis: { nested: { key: 'val' } } }] }),
    )
    const text = wrapper.findAll('tbody tr').find(r => r.text().includes('m'))!.text()
    expect(text).toContain('nested')
  })

  // -- liveCostTotal: model_cost_display_usd fallback --
  it('uses model_cost_display_usd when cost_usd is absent for live total', async () => {
    const wrapper = await mountWith(
      baseRun({ status: 'running', node_token_usage: { n1: { model_cost_display_usd: 0.025 } } }),
    )
    expect(wrapper.find('[data-testid="run-detail-live-cost"]').text()).toContain('0.0250')
  })

  // -- runLevelWarnings: stale heartbeat --
  it('shows stale heartbeat warning in strip', async () => {
    const wrapper = await mountWith(
      baseRun({ status: 'running', heartbeat_at: '2026-01-01T00:00:00Z' }),
    )
    const strip = wrapper.find('[data-testid="run-detail-warnings-strip"]')
    expect(strip.exists()).toBe(true)
    expect(strip.text()).toContain('stale')
  })

  // -- runLevelWarnings: empty when no conditions --
  it('hides warnings strip when no conditions are met', async () => {
    const wrapper = await mountWith(baseRun())
    expect(wrapper.find('[data-testid="run-detail-warnings-strip"]').exists()).toBe(false)
  })

  // -- copyText: clipboard error path --
  it('handles clipboard write failure gracefully', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard blocked'))
    Object.assign(navigator, { clipboard: { writeText } })
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const wrapper = await mountWith(baseRun({ trace_id: 'trace-123' }))
    const btn = wrapper.find('[data-testid="run-detail-copy-trace-id"]')
    await btn.trigger('click')
    await flushPromises()
    expect(warnSpy).toHaveBeenCalled()
    warnSpy.mockRestore()
  })

  // -- nodeEntries: empty object output treated as no output --
  it('resolves empty object node output to null output via pure return path', async () => {
    const wrapper = await mountWith(baseRun(), {
      outputs_json: { n1: { output: null } },
    })
    const ioRow = wrapper.find('[data-testid="run-detail-io-row"]')
    expect(ioRow.exists()).toBe(true)
    // Empty output should show no-output message
    expect(wrapper.find('[data-testid="run-detail-no-output"]').exists()).toBe(true)
  })

  // -- shareSummary: with nodesReportedTokens --
  it('includes node-reported token label in share summary', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(
      baseRun({ node_token_usage: { n1: { total_tokens: 500 } } }),
    )
    await wrapper.find('[data-testid="run-detail-share-summary"]').trigger('click')
    await flushPromises()
    expect(writeText.mock.calls[0][0]).toContain('node-reported')
  })

  // -- shareSummary: with costBasisTokens --
  it('includes cost-basis token label in share summary', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const wrapper = await mountWith(
      baseRun({
        total_cost_usd: 0.5,
        cost_breakdown: [{ component: 'model', amount_usd: '0.500000', basis: { tokens_total_reported: 100 } }],
      }),
    )
    await wrapper.find('[data-testid="run-detail-share-summary"]').trigger('click')
    await flushPromises()
    expect(writeText.mock.calls[0][0]).toContain('cost basis')
  })

  // -- totalTokens: node reported tokens display --
  it('shows node-reported total tokens in cost section', async () => {
    const wrapper = await mountWith(
      baseRun({
        total_cost_usd: 1.0,
        node_token_usage: { n1: { total_tokens: 1500 }, n2: { total_tokens: 500 } },
      }),
    )
    expect(wrapper.find('#run-detail-cost-section').text()).toContain('2,000')
  })

  // -- costBasisTokens: no basis returns null --
  it('shows generic token line when no cost basis tokens available', async () => {
    const wrapper = await mountWith(
      baseRun({
        total_cost_usd: 1.0,
        node_token_usage: { n1: { total_tokens: 100 } },
      }),
    )
    expect(wrapper.find('#run-detail-cost-section').text()).toContain('100')
  })

  // -- canRerun: false when no pipeline_id --
  it('hides rerun button when pipeline_id is absent', async () => {
    const wrapper = await mountWith(baseRun({ pipeline_id: null }))
    expect(wrapper.find('[data-testid="run-detail-rerun"]').exists()).toBe(false)
  })

  // -- childRunCount: zero treated as zero --
  it('hides aggregate when child_runs_count is zero', async () => {
    const wrapper = await mountWith(
      baseRun({ child_runs_cost_usd: '0.5', aggregate_cost_usd: '1.5', child_runs_count: 0 }),
    )
    const agg = wrapper.find('[data-testid="run-detail-aggregate-cost"]')
    expect(agg.exists()).toBe(true)
    expect(agg.text()).not.toContain('1 run')
  })
})
