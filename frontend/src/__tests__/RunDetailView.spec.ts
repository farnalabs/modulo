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
          return Promise.resolve({ data: { outputs_json: { 'node-a': { input: { q: 'hello' }, output: 'response' } } }, error: undefined })
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

  it('shows prompt hidden state for nodes', async () => {
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()
    const revealBtns = wrapper.findAll('[data-testid="run-detail-reveal-prompt"]')
    expect(revealBtns.length).toBeGreaterThanOrEqual(1)
    expect(revealBtns[0].text()).toContain('Prompt hidden')
    wrapper.unmount()
  })

  it('reveals prompt on click and shows dialog', async () => {
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await flushPromises()
    await nextTick()

    const revealBtn = wrapper.find('[data-testid="run-detail-reveal-prompt"]')
    expect(revealBtn.exists()).toBe(true)
    await revealBtn.trigger('click')
    await nextTick()
    await flushPromises()

    const showBtn = wrapper.find('[data-testid="run-detail-show-prompt"]')
    expect(showBtn.exists()).toBe(true)
    await showBtn.trigger('click')
    await nextTick()

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

    const revealBtn = wrapper.find('[data-testid="run-detail-reveal-prompt"]')
    await revealBtn.trigger('click')
    await nextTick()
    await flushPromises()

    const showBtn = wrapper.find('[data-testid="run-detail-show-prompt"]')
    await showBtn.trigger('click')
    await nextTick()

    const copyBtn = document.querySelector('[data-testid="run-detail-copy-prompt"]')
    expect(copyBtn).not.toBeNull()
    ;(copyBtn as HTMLElement).click()
    expect(writeText).toHaveBeenCalled()
    expect(writeText.mock.calls[0][0]).toContain('helpful assistant')
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
})
