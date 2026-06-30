import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
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
    await nextTick()
    const btn = wrapper.find('[data-testid="diff-compare-btn"]')
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('enables compare button when all inputs are filled', async () => {
    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
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
    await nextTick()

    await wrapper.find('[data-testid="diff-run-id-a"]').setValue('run-a')
    await wrapper.find('[data-testid="diff-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="diff-run-id-b"]').setValue('run-b')
    await wrapper.find('[data-testid="diff-compare-btn"]').trigger('click')

    await nextTick()
    await new Promise(r => setTimeout(r, 0))

    expect(wrapper.text()).toContain('1 added')
    expect(wrapper.text()).toContain('1 removed')
  })

  it('shows identical banner when has_diff is false', async () => {
    vi.mocked(api.POST).mockResolvedValue({
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
    vi.mocked(api.POST).mockResolvedValue({
      data: null,
      error: { status: 404, detail: 'Run not found' },
    })

    router.push('/runs/diff')
    await router.isReady()
    const wrapper = mount(AgentOutputDiffView, {
      global: { plugins: [router] },
    })
    await nextTick()

    await wrapper.find('[data-testid="diff-run-id-a"]').setValue('run-a')
    await wrapper.find('[data-testid="diff-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="diff-run-id-b"]').setValue('run-b')
    await wrapper.find('[data-testid="diff-compare-btn"]').trigger('click')

    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    expect(wrapper.text()).toContain('Diff failed')
  })
})
