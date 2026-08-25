import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: postMock,
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue({ events: [] }), post: vi.fn(), put: vi.fn(), delete: vi.fn(), patch: vi.fn() }),
}))

vi.mock('../lib/jwt', () => ({
  decodeJwtPayload: () => ({ org_role: 'admin' }),
}))

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

vi.mock('../lib/api/runs', () => ({
  requestRunCancellation: vi.fn(),
}))

import RunDetailView from '../views/RunDetailView.vue'
import { api } from '../lib/api/client'

function guardrailBlockedRun() {
  return {
    run_id: 'test-run-id',
    pipeline_id: 'test-pipeline',
    status: 'eval_failed',
    error_code: 'eval_blocked',
    error_detail: 'Blocked by guardrail block_credit_card',
    guardrail_summary: {
      evaluated: 3,
      passed: 2,
      violated: 1,
      observed: 1,
      errored: 0,
      redacted: 0,
      skipped: 0,
    },
    node_token_usage: {},
  }
}

function mountView(run: unknown = guardrailBlockedRun()) {
  ;(api.GET as any).mockImplementation((url: string) => {
    if (url === '/api/v1/runs/{run_id}') {
      return Promise.resolve({ data: run, error: undefined })
    }
    if (url === '/api/v1/runs/{run_id}/io') {
      return Promise.resolve({ data: { outputs_json: {}, node_telemetry: {}, input_payload: {} }, error: undefined })
    }
    if (url === '/api/v1/runs/{run_id}/workspace-lease') {
      return Promise.resolve({ data: null, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
  return mount(RunDetailView, {
    global: {
      stubs: {
        Dialog: {
          template: '<div class="p-dialog"><slot name="header" /><slot /><slot name="footer" /></div>',
        },
        Button: { template: '<button type="button"><slot /></button>' },
        JsonViewer: { template: '<div />' },
        RunErrorTag: { template: '<span />' },
        LoadingSpinner: { template: '<div />' },
        ErrorAlert: { template: '<div />' },
        PageHeader: { template: '<div />' },
      },
    },
  })
}

async function flush() {
  await flushPromises()
  await nextTick()
  await nextTick()
  await nextTick()
}

describe('RunDetailView guardrail summary + override', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    postMock.mockResolvedValue({ data: { run_id: 'test-run-id', status: 'pending', action: 'override' }, error: undefined })
  })

  it('renders the guardrail summary card with buckets', async () => {
    const wrapper = mountView()
    await flush()

    const card = wrapper.find('[data-testid="run-detail-guardrail-summary"]')
    expect(card.exists()).toBe(true)
    const buckets = wrapper.findAll('[data-testid="run-detail-guardrail-bucket"]')
    // evaluated, passed, violated, observed are > 0 (errored/redacted/skipped are 0).
    expect(buckets.length).toBe(4)
  })

  it('shows the override panel and button for a guardrail-blocked run', async () => {
    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-testid="run-detail-guardrail-override-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-override-guardrail"]').exists()).toBe(true)
  })

  it('submitting the override POSTs the corrected input to the guardrail-override endpoint', async () => {
    const wrapper = mountView()
    await flush()

    const textarea = wrapper.find('[data-testid="run-detail-override-input"]')
    expect(textarea.exists()).toBe(true)
    await textarea.setValue('{ "query": "safe" }')
    await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
    await flush()

    expect(postMock).toHaveBeenCalledTimes(1)
    const [url, opts] = postMock.mock.calls[0]
    expect(url).toBe('/api/v1/runs/{run_id}/guardrail-override')
    expect(opts.params.path.run_id).toBe('test-run-id')
    expect(opts.body).toEqual({ input_data: { query: 'safe' } })
  })

  it('shows the re-block error when the corrected input still violates (422)', async () => {
    postMock.mockResolvedValue({
      data: null,
      error: { status: 422, detail: 'still violates' },
    })
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="run-detail-override-input"]').setValue('{ "query": "blocked" }')
    await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
    await flush()

    const err = wrapper.find('[data-testid="run-detail-override-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('still violates')
  })

  it('hides the guardrail summary card when the run has no guardrail_summary', async () => {
    const noSummary = { ...guardrailBlockedRun(), guardrail_summary: undefined }
    const wrapper = mountView(noSummary)
    await flush()

    expect(wrapper.find('[data-testid="run-detail-guardrail-summary"]').exists()).toBe(false)
  })

  it('shows a success message and clears the override dialog after a successful override', async () => {
    postMock.mockResolvedValue({ data: { run_id: 'test-run-id', status: 'pending', action: 'override' }, error: undefined })
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="run-detail-override-input"]').setValue('{ "query": "safe" }')
    await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
    await flush()

    const ok = wrapper.find('[data-testid="run-detail-override-success"]')
    expect(ok.exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-override-error"]').exists()).toBe(false)
    expect(postMock).toHaveBeenCalledTimes(1)
  })

  it('rejects invalid JSON input without calling the override endpoint', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="run-detail-override-input"]').setValue('{ not valid json')
    await wrapper.find('[data-testid="run-detail-override-submit"]').trigger('click')
    await flush()

    const err = wrapper.find('[data-testid="run-detail-override-error"]')
    expect(err.exists()).toBe(true)
    expect(postMock).not.toHaveBeenCalled()
  })
})
