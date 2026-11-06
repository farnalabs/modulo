import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    POST: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/runs/diff') {
        return Promise.resolve({
          data: {
            run_id_a: 'aaa',
            run_id_b: 'bbb',
            node_output_a: { result: 'hello' },
            node_output_b: { result: 'world' },
            diff_lines: [
              { type: 'unchanged', content: '{', line_a: 1, line_b: 1 },
              { type: 'removed', content: '  "result": "hello"', line_a: 2, line_b: null },
              { type: 'added', content: '  "result": "world"', line_a: null, line_b: 2 },
              { type: 'unchanged', content: '}', line_a: 3, line_b: 3 },
            ],
            has_diff: true,
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import AgentOutputDiffView from '../views/AgentOutputDiffView.vue'

vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
  ok: true,
  status: 200,
  json: () => Promise.resolve({ recent_runs: [] }),
}))

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/runs/diff', name: 'runs-diff', component: AgentOutputDiffView },
  ],
})

describe('AgentOutputDiffView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Agent Output Diff')
  })

  it('disables compare button when inputs are empty', async () => {
    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    const btn = wrapper.find('[data-testid="diff-compare-btn"]')
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('stacks diff controls at narrow widths while preserving desktop widths', async () => {
    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    const controls = wrapper.find('.rounded-lg.border.bg-card.p-6 > .flex')
    expect(controls.classes()).toEqual(expect.arrayContaining(['flex-col', 'sm:flex-row']))

    const selects = wrapper.findAllComponents({ name: 'Select' })
    expect(selects).toHaveLength(2)
    expect(selects[0].classes()).toEqual(expect.arrayContaining(['w-full', 'sm:w-52']))
    expect(wrapper.find('[data-testid="diff-run-id-a"]').classes()).toEqual(
      expect.arrayContaining(['w-full', 'sm:w-48']),
    )
    expect(wrapper.find('[data-testid="diff-compare-btn"]').classes()).toEqual(
      expect.arrayContaining(['w-full', 'sm:w-auto']),
    )
  })

  it('enables compare button when all inputs are filled', async () => {
    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="diff-run-id-a"]').setValue('run-a')
    await wrapper.find('[data-testid="diff-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="diff-run-id-b"]').setValue('run-b')
    const btn = wrapper.find('[data-testid="diff-compare-btn"]')
    expect(btn.attributes('disabled')).toBeUndefined()
  })

  it('calls API and renders diff results on compare', async () => {
    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="diff-run-id-a"]').setValue('run-a')
    await wrapper.find('[data-testid="diff-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="diff-run-id-b"]').setValue('run-b')
    await wrapper.find('[data-testid="diff-compare-btn"]').trigger('click')

    await flushPromises()
    await nextTick()
    await new Promise(r => setTimeout(r, 0))

    expect(wrapper.text()).toContain('1 added')
    expect(wrapper.text()).toContain('1 removed')
  })

  it('shows identical banner when has_diff is false', async () => {
    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockResolvedValue({
      data: {
        run_id_a: 'aaa',
        run_id_b: 'bbb',
        node_output_a: { result: 'hello' },
        node_output_b: { result: 'hello' },
        diff_lines: [
          { type: 'unchanged', content: '{', line_a: 1, line_b: 1 },
          { type: 'unchanged', content: '  "result": "hello"', line_a: 2, line_b: 2 },
          { type: 'unchanged', content: '}', line_a: 3, line_b: 3 },
        ],
        has_diff: false,
      },
      error: undefined,
    })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="diff-run-id-a"]').setValue('run-a')
    await wrapper.find('[data-testid="diff-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="diff-run-id-b"]').setValue('run-b')
    await wrapper.find('[data-testid="diff-compare-btn"]').trigger('click')

    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    expect(wrapper.find('[data-testid="diff-identical-banner"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Outputs are identical')
  })

  it('shows error when API returns an error', async () => {
    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockResolvedValue({
      data: null,
      error: { status: 404, detail: 'Run not found' },
    })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="diff-run-id-a"]').setValue('run-a')
    await wrapper.find('[data-testid="diff-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="diff-run-id-b"]').setValue('run-b')
    await wrapper.find('[data-testid="diff-compare-btn"]').trigger('click')

    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    expect(wrapper.text()).toContain('Run not found')
  })

  it('offers Retry on a compare failure and re-runs the compare on click (FAR-1655)', async () => {
    // First compare fails; Retry (which re-invokes handleCompare) succeeds.
    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation(async (url: string) => {
      if (url === '/api/v1/runs/diff') {
        const callCount = vi.mocked(api.POST).mock.calls.filter((c) => c[0] === '/api/v1/runs/diff').length
        if (callCount <= 1) return { data: null, error: { status: 500, detail: 'diff backend down' } }
        return {
          data: {
            run_id_a: 'aaa',
            run_id_b: 'bbb',
            node_output_a: { result: 'hello' },
            node_output_b: { result: 'world' },
            diff_lines: [{ type: 'added', content: 'world', line_a: null, line_b: 1 }],
            has_diff: true,
          },
          error: undefined,
        }
      }
      return { data: null, error: undefined }
    })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="diff-run-id-a"]').setValue('run-a')
    await wrapper.find('[data-testid="diff-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="diff-run-id-b"]').setValue('run-b')
    await wrapper.find('[data-testid="diff-compare-btn"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('diff backend down')
    const retry = wrapper.find('[data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)

    await retry.trigger('click')
    await flushPromises()
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    // The retried compare succeeded: POST /runs/diff called twice, error cleared.
    const diffCalls = vi.mocked(api.POST).mock.calls.filter((c) => c[0] === '/api/v1/runs/diff')
    expect(diffCalls).toHaveLength(2)
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('diff backend down')
  })

  it('shows a distinct retryable error (not the empty state) when the summary fetch fails', async () => {
    // FAR-2366: a failed summary fetch used to be swallowed and rendered as
    // "no recent runs". It must surface as an error with a retry instead.
    vi.mocked(fetch as unknown as () => Promise<unknown>).mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: () => Promise.resolve({ detail: 'summary unavailable' }),
    })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="diff-recent-runs-empty"]').exists()).toBe(false)
    const alerts = wrapper.findAll('[role="alert"]')
    expect(alerts).toHaveLength(1)
    expect(alerts[0].text()).toContain("Couldn't load recent runs.")
    expect(alerts[0].text()).toContain('summary unavailable')
    expect(alerts[0].find('[data-testid="error-alert-retry"]').exists()).toBe(true)

    const selects = wrapper.findAllComponents({ name: 'Select' })
    expect(selects.length).toBeGreaterThanOrEqual(2)
    expect(selects[0].props('options')).toEqual([])
    expect(selects[1].props('options')).toEqual([])
    expect(selects[0].props('disabled')).toBe(true)
    expect(selects[1].props('disabled')).toBe(true)
  })

  it('retries the summary fetch from the error state and recovers the run list', async () => {
    vi.mocked(fetch as unknown as () => Promise<unknown>)
      .mockResolvedValueOnce({
        ok: false,
        status: 500,
        statusText: 'Internal Server Error',
        json: () => Promise.resolve({ detail: 'summary unavailable' }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          recent_runs: [
            { id: 'run-a', pipeline_name: 'Build', status: 'complete', created_at: '2026-11-04T10:00:00Z' },
          ],
        }),
      })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    const retry = wrapper.find('[role="alert"] [data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)
    await retry.trigger('click')
    await flushPromises()
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="diff-recent-runs-empty"]').exists()).toBe(false)
    const selects = wrapper.findAllComponents({ name: 'Select' })
    expect(selects[0].props('options')).toEqual([
      { value: 'run-a', label: 'Build — complete (2026-11-04T10:00:00Z)' },
    ])
    expect(selects[0].props('disabled')).toBe(false)
  })

  it('shows the empty state when the summary fetch succeeds with zero runs', async () => {
    vi.mocked(fetch as unknown as () => Promise<unknown>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ recent_runs: [] }),
    })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    const empty = wrapper.find('[data-testid="diff-recent-runs-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.attributes('aria-live')).toBe('polite')
    expect(empty.text()).toContain('10 most recent runs of any status')
    expect(empty.text()).toContain('recorded output for the same node')
  })

  it('populates both selectors from the dashboard recent-runs payload', async () => {
    vi.mocked(fetch as unknown as () => Promise<unknown>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        recent_runs: [
          { id: 'run-a', pipeline_name: 'Build', status: 'complete', created_at: '2026-11-04T10:00:00Z' },
          { id: 'run-b', pipeline_name: 'Build', status: 'failed', created_at: '2026-11-04T09:00:00Z' },
        ],
      }),
    })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    const selects = wrapper.findAllComponents({ name: 'Select' })
    expect(selects).toHaveLength(2)
    const expectedOptions = [
      { value: 'run-a', label: 'Build — complete (2026-11-04T10:00:00Z)' },
      { value: 'run-b', label: 'Build — failed (2026-11-04T09:00:00Z)' },
    ]
    expect(selects[0].props('options')).toEqual(expectedOptions)
    expect(selects[1].props('options')).toEqual(expectedOptions)
    expect(selects[0].props('disabled')).toBe(false)
    expect(selects[1].props('disabled')).toBe(false)
    expect(wrapper.find('[data-testid="diff-recent-runs-empty"]').exists()).toBe(false)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('uses the viewer dashboard endpoint for recent runs', async () => {
    router.push('/runs/diff')
    await router.isReady()
    mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()

    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      '/api/v1/dashboard/summary',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('renders pure-return node outputs with no telemetry keys', async () => {
    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockResolvedValue({
      data: {
        run_id_a: 'aaa',
        run_id_b: 'bbb',
        node_output_a: { summary: 'x', result: 'ok' },
        node_output_b: { summary: 'y', result: 'ok' },
        diff_lines: [
          { type: 'unchanged', content: '{', line_a: 1, line_b: 1 },
          { type: 'removed', content: '  "summary": "x"', line_a: 2, line_b: null },
          { type: 'added', content: '  "summary": "y"', line_a: null, line_b: 2 },
          { type: 'unchanged', content: '}', line_a: 3, line_b: 3 },
        ],
        has_diff: true,
      },
      error: undefined,
    })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="diff-run-id-a"]').setValue('run-a')
    await wrapper.find('[data-testid="diff-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="diff-run-id-b"]').setValue('run-b')
    await wrapper.find('[data-testid="diff-compare-btn"]').trigger('click')

    await flushPromises()
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    const viewers = wrapper.findAll('[data-testid="json-viewer"]')
    expect(viewers.length).toBeGreaterThanOrEqual(2)
    const text = viewers.map(v => v.text()).join(' ')
    expect(text).toContain('summary')
    expect(text).toContain('result')
    expect(text).toContain('ok')
    expect(text).not.toContain('agent_stdout')
    expect(text).not.toContain('agent_stderr')
    expect(text).not.toContain('exit_code')
  })

  it('shows a pulse skeleton while the compare request is in flight (STATE-3)', async () => {
    let resolvePost: (value: unknown) => void = () => {}
    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockImplementation(
      () => new Promise((resolve) => { resolvePost = resolve }),
    )

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="diff-run-id-a"]').setValue('run-a')
    await wrapper.find('[data-testid="diff-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="diff-run-id-b"]').setValue('run-b')
    await wrapper.find('[data-testid="diff-compare-btn"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="diff-comparing"]').exists()).toBe(true)

    resolvePost({
      data: {
        run_id_a: 'run-a',
        run_id_b: 'run-b',
        node_output_a: { result: 'hello' },
        node_output_b: { result: 'world' },
        diff_lines: [{ type: 'added', content: 'world', line_a: null, line_b: 1 }],
        has_diff: true,
      },
      error: undefined,
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="diff-comparing"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('1 added')
  })

  it('renders recent-run option labels through i18n (I18N-1)', async () => {
    vi.mocked(fetch as unknown as () => Promise<unknown>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        recent_runs: [
          { id: 'run-1', pipeline_name: 'Alpha', status: 'failed', created_at: '2026-01-02T03:04:05Z' },
        ],
      }),
    })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()

    const selects = wrapper.findAllComponents({ name: 'Select' })
    expect(selects.length).toBeGreaterThanOrEqual(2)
    expect(selects[0].props('options')).toEqual([
      { value: 'run-1', label: 'Alpha — failed (2026-01-02T03:04:05Z)' },
    ])
  })
})
