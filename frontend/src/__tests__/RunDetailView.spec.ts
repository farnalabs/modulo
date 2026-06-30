import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
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
    await new Promise(r => setTimeout(r, 0))
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Run Detail')
    wrapper.unmount()
  })

  it('shows prompt hidden state for nodes', async () => {
    router.push('/runs/test-run-id')
    await router.isReady()
    const wrapper = createWrapper()
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
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
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    const revealBtn = wrapper.find('[data-testid="run-detail-reveal-prompt"]')
    expect(revealBtn.exists()).toBe(true)
    await revealBtn.trigger('click')
    await nextTick()
    await new Promise(r => setTimeout(r, 0))

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
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    const revealBtn = wrapper.find('[data-testid="run-detail-reveal-prompt"]')
    await revealBtn.trigger('click')
    await nextTick()
    await new Promise(r => setTimeout(r, 0))

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
})
