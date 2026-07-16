import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/stages') {
        return Promise.resolve({ data: {
          items: [
            { id: 'stage-1', name: 'Development', description: 'Build stage', position: 0, visibility: 'org', owner_team_id: null, created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' },
            { id: 'stage-2', name: 'Testing', description: 'QA stage', position: 1, visibility: 'org', owner_team_id: 'team-alpha', created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' },
            { id: 'stage-3', name: 'Production', description: 'Live stage', position: 2, visibility: 'team', owner_team_id: 'team-beta', created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' },
          ],
          total: 3,
        }, error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({ data: {
          items: [
            { id: 'pipeline-1', name: 'Feature X', status: 'running', stage_id: 'stage-1', team_name: 'Alpha', created_at: '2026-06-28T10:00:00Z' },
            { id: 'pipeline-2', name: 'Bugfix Y', status: 'complete', stage_id: 'stage-2', team_name: 'Beta', created_at: '2026-06-27T10:00:00Z' },
            { id: 'pipeline-3', name: 'Release Z', status: 'idle', stage_id: 'stage-3', team_name: 'Alpha', created_at: '2026-06-26T10:00:00Z' },
            { id: 'pipeline-4', name: 'Refactor A', status: 'failed', stage_id: 'stage-1', team_name: 'Beta', created_at: '2026-06-25T10:00:00Z' },
          ],
          total: 4,
        }, error: undefined })
      }
      if (url === '/api/v1/teams') {
        return Promise.resolve({ data: {
          items: [
            { id: 'team-alpha', name: 'Alpha' },
            { id: 'team-beta', name: 'Beta' },
          ],
        }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    }),
    POST: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
}))

import StageBoardView from '../views/StageBoardView.vue'

describe('StageBoardView', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    vi.clearAllMocks()
  })

  function mountStageBoard() {
    return mount(StageBoardView, {
      global: {
        plugins: [pinia],
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
      },
    })
  }

  it('renders without crashing', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
  })

  it('renders the heading', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Stage Board')
  })

  it('renders stage columns', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Development')
    expect(wrapper.text()).toContain('Testing')
    expect(wrapper.text()).toContain('Production')
  })

  it('shows pipeline cards within stage columns', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Feature X')
    expect(wrapper.text()).toContain('Bugfix Y')
    expect(wrapper.text()).toContain('Release Z')
    expect(wrapper.text()).toContain('Refactor A')
  })

  it('shows status badges on pipeline cards', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('running')
    expect(wrapper.text()).toContain('complete')
    expect(wrapper.text()).toContain('failed')
    expect(wrapper.text()).toContain('idle')
  })

  it('shows filters', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="stage-board-team-filter"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-status-filter"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-date-from"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-date-to"]').exists()).toBe(true)
  })

  it('shows create stage button', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="stage-board-create-btn"]').exists()).toBe(true)
  })

  it('opens create stage dialog on button click', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="stage-board-create-btn"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="stage-board-create-name"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-create-submit"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stage-board-create-cancel"]').exists()).toBe(true)
  })

  it('shows stage detail panel on column click', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="stage-board-column-stage-1"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Stage Details')
    expect(wrapper.text()).toContain('Development')
  })

  it('shows pipeline detail panel on card click', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="stage-board-card-pipeline-1"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Pipeline Details')
    expect(wrapper.text()).toContain('Feature X')
  })

  it('shows all stages when no team filter is selected', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Development')
    expect(wrapper.text()).toContain('Testing')
    expect(wrapper.text()).toContain('Production')
  })

  it('filters stages by team when a team is selected', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    ;(wrapper.vm as any).teamFilter = 'team-alpha'
    await nextTick()
    expect(wrapper.text()).toContain('Testing')
    expect(wrapper.text()).not.toContain('Production')
  })

  it('shows all stages when switching back to All Teams', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    ;(wrapper.vm as any).teamFilter = 'team-alpha'
    await nextTick()
    expect(wrapper.text()).not.toContain('Production')
    ;(wrapper.vm as any).teamFilter = '__all__'
    await nextTick()
    expect(wrapper.text()).toContain('Development')
    expect(wrapper.text()).toContain('Testing')
    expect(wrapper.text()).toContain('Production')
  })

  it('populates team filter dropdown with loaded teams', async () => {

    const wrapper = mountStageBoard()

    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('All Teams')
    expect(wrapper.text()).toContain('Alpha')
    expect(wrapper.text()).toContain('Beta')
  })
})
