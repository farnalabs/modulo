import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/stages') {
        return Promise.resolve({
          items: [
            { id: 'stage-1', name: 'Development', description: 'Build stage', position: 0, visibility: 'org', created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' },
            { id: 'stage-2', name: 'Testing', description: 'QA stage', position: 1, visibility: 'org', created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' },
            { id: 'stage-3', name: 'Production', description: 'Live stage', position: 2, visibility: 'team', created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' },
          ],
          total: 3,
        })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          items: [
            { id: 'pipeline-1', name: 'Feature X', status: 'running', stage_id: 'stage-1', team_name: 'Alpha', created_at: '2026-06-28T10:00:00Z' },
            { id: 'pipeline-2', name: 'Bugfix Y', status: 'complete', stage_id: 'stage-2', team_name: 'Beta', created_at: '2026-06-27T10:00:00Z' },
            { id: 'pipeline-3', name: 'Release Z', status: 'idle', stage_id: 'stage-3', team_name: 'Alpha', created_at: '2026-06-26T10:00:00Z' },
            { id: 'pipeline-4', name: 'Refactor A', status: 'failed', stage_id: 'stage-1', team_name: 'Beta', created_at: '2026-06-25T10:00:00Z' },
          ],
          total: 4,
        })
      }
      if (url === '/api/v1/teams') {
        return Promise.resolve({
          items: [
            { id: 'team-alpha', name: 'Alpha' },
            { id: 'team-beta', name: 'Beta' },
          ],
        })
      }
      return Promise.resolve({ items: [] })
    }),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
  })),
}))

import StageBoardView from '../views/StageBoardView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/stages', name: 'stages', component: StageBoardView },
  ],
})

function flushPromises() {
  return new Promise(resolve => setTimeout(resolve, 0))
}

describe('StageBoardView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
  })

  it('renders the heading', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Stage Board')
  })

  it('renders stage columns', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Development')
    expect(wrapper.text()).toContain('Testing')
    expect(wrapper.text()).toContain('Production')
  })

  it('shows pipeline cards within stage columns', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Feature X')
    expect(wrapper.text()).toContain('Bugfix Y')
    expect(wrapper.text()).toContain('Release Z')
    expect(wrapper.text()).toContain('Refactor A')
  })

  it('shows status badges on pipeline cards', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('running')
    expect(wrapper.text()).toContain('complete')
    expect(wrapper.text()).toContain('failed')
    expect(wrapper.text()).toContain('idle')
  })

  it('shows filters', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="stage-board-team-filter"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-status-filter"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-date-from"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-date-to"]').exists()).toBe(true)
  })

  it('shows create stage button', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="stage-board-create-btn"]').exists()).toBe(true)
  })

  it('opens create stage dialog on button click', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="stage-board-create-btn"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="stage-board-create-name"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-create-submit"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-create-cancel"]').exists()).toBe(true)
  })

  it('shows stage detail panel on column click', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="stage-board-column-stage-1"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Stage Details')
    expect(wrapper.text()).toContain('Development')
  })

  it('shows pipeline detail panel on card click', async () => {
    router.push('/stages')
    await router.isReady()
    const wrapper = mount(StageBoardView, {
      global: { plugins: [router] },
    })
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="stage-board-card-pipeline-1"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Pipeline Details')
    expect(wrapper.text()).toContain('Feature X')
  })
})
