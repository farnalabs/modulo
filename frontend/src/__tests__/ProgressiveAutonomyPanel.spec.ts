import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ProgressiveAutonomyPanel from '../components/pipeline/ProgressiveAutonomyPanel.vue'

const apiMocks = vi.hoisted(() => ({
  post: vi.fn(),
}))

vi.mock('@/lib/api/client', () => ({
  api: {
    GET: vi.fn(() => Promise.resolve({ data: null, error: undefined })),
    POST: apiMocks.post,
    PATCH: vi.fn(() => Promise.resolve({ data: null, error: undefined })),
  },
}))

type PanelProps = {
  pipelineId?: string
  defaultLevel?: string | null
  maxLevel?: string | null
  earnedLevel?: string | null
  earnedUpdatedAt?: string | null
}

function mountPanel(props: PanelProps = {}): VueWrapper {
  return mount(ProgressiveAutonomyPanel, {
    props: {
      pipelineId: 'pipe-1',
      defaultLevel: 'manual_approval',
      maxLevel: 'fully_autonomous',
      earnedLevel: 'notify_on_complete',
      earnedUpdatedAt: '2026-09-01T10:00:00Z',
      ...props,
    },
  })
}

function successBody(level: string, previousLevel: string) {
  return {
    pipeline_id: 'pipe-1',
    previous_level: previousLevel,
    earned_autonomy_level: level,
    max_autonomy_level: 'fully_autonomous',
    event_type: 'pipeline.autonomy_demoted',
    updated_at: '2026-09-02T11:22:33Z',
  }
}

describe('ProgressiveAutonomyPanel', () => {
  beforeEach(() => {
    apiMocks.post.mockReset()
  })

  it('renders the default, ceiling and earned levels with their labels', () => {
    const wrapper = mountPanel()

    const defaultRow = wrapper.find('[data-testid="progressive-autonomy-default"]')
    const ceilingRow = wrapper.find('[data-testid="progressive-autonomy-ceiling"]')
    const earnedRow = wrapper.find('[data-testid="progressive-autonomy-earned"]')

    expect(defaultRow.text()).toContain('Manual approval')
    expect(ceilingRow.text()).toContain('Fully autonomous')
    expect(earnedRow.text()).toContain('Notify on complete')
    expect(wrapper.find('[data-testid="progressive-autonomy-earned-at"]').text()).toContain('Set')
    expect(wrapper.find('[data-testid="progressive-autonomy-panel"]').exists()).toBe(true)
  })

  it('renders an explicit fallback when the earned level is null', () => {
    const wrapper = mountPanel({ earnedLevel: null, earnedUpdatedAt: null })

    const earnedRow = wrapper.find('[data-testid="progressive-autonomy-earned"]')
    expect(earnedRow.text()).toContain('Not set - using the pipeline default')
    expect(wrapper.find('[data-testid="progressive-autonomy-earned-at"]').exists()).toBe(false)
  })

  it('demotes with {level, reason} and reports the change on 200', async () => {
    apiMocks.post.mockResolvedValue({
      data: successBody('manual_approval', 'notify_on_complete'),
      error: undefined,
      response: { status: 200 },
    })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('manual_approval')
    await wrapper.find('[data-testid="progressive-autonomy-reason"]').setValue('incident 42 review')
    await wrapper.find('[data-testid="progressive-autonomy-demote"]').trigger('click')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledTimes(1)
    expect(apiMocks.post).toHaveBeenCalledWith('/api/v1/pipelines/{pipeline_id}/autonomy/demote', {
      params: { path: { pipeline_id: 'pipe-1' } },
      body: { level: 'manual_approval', reason: 'incident 42 review' },
    })
    // The panel reflects the new earned level straight from the response and
    // tells the editor to refetch the pipeline.
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Manual approval',
    )
    expect(wrapper.find('[data-testid="progressive-autonomy-status"]').text()).toContain(
      'Autonomy demoted to Manual approval',
    )
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })

  it('promotes with {level} (no reason key) and reports the change on 200', async () => {
    apiMocks.post.mockResolvedValue({
      data: successBody('fully_autonomous', 'notify_on_complete'),
      error: undefined,
      response: { status: 200 },
    })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('fully_autonomous')
    await wrapper.find('[data-testid="progressive-autonomy-promote"]').trigger('click')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith('/api/v1/pipelines/{pipeline_id}/autonomy/promote', {
      params: { path: { pipeline_id: 'pipe-1' } },
      body: { level: 'fully_autonomous' },
    })
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Fully autonomous',
    )
    expect(wrapper.find('[data-testid="progressive-autonomy-status"]').text()).toContain(
      'Autonomy promoted to Fully autonomous',
    )
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })

  it('never offers a target above the ceiling', async () => {
    const wrapper = mountPanel({ earnedLevel: 'manual_approval', maxLevel: 'notify_on_complete' })

    const target = wrapper.find('[data-testid="progressive-autonomy-target"]')
    const options = target.findAll('option')
    const ceilingOption = options.find((o) => o.element.value === 'fully_autonomous')

    expect(ceilingOption).toBeDefined()
    expect(ceilingOption!.attributes('disabled')).toBeDefined()

    // A legal target still enables promote; the above-ceiling one never does.
    await target.setValue('notify_on_complete')
    expect(
      wrapper.find('[data-testid="progressive-autonomy-promote"]').attributes('disabled'),
    ).toBeUndefined()
    await target.setValue('fully_autonomous')
    expect(
      wrapper.find('[data-testid="progressive-autonomy-promote"]').attributes('disabled'),
    ).toBeDefined()
  })

  it('surfaces a 403 refusal with a specific not-permitted message', async () => {
    apiMocks.post.mockResolvedValue({
      error: { detail: 'not an admin or owner' },
      response: { status: 403 },
    })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('manual_approval')
    await wrapper.find('[data-testid="progressive-autonomy-demote"]').trigger('click')
    await flushPromises()

    const error = wrapper.find('[data-testid="progressive-autonomy-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('Only an organisation admin or the pipeline')
    expect(error.text()).toContain('not an admin or owner')
    expect(wrapper.emitted('changed')).toBeUndefined()
    // The displayed earned level was never touched by the refusal.
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Notify on complete',
    )
  })

  it('surfaces the 422 server detail when a promotion is refused', async () => {
    const serverDetail =
      "Promotion to fully_autonomous exceeds the pipeline's max_autonomy_level ceiling (notify_on_complete)"
    apiMocks.post.mockResolvedValue({
      error: { detail: serverDetail },
      response: { status: 422 },
    })
    // The UI allows the target (the ceiling it was rendered with permits it)
    // but the server refuses — e.g. the ceiling moved after the panel loaded.
    const wrapper = mountPanel({ maxLevel: 'fully_autonomous', earnedLevel: 'notify_on_complete' })

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('fully_autonomous')
    expect(
      wrapper.find('[data-testid="progressive-autonomy-promote"]').attributes('disabled'),
    ).toBeUndefined()
    await wrapper.find('[data-testid="progressive-autonomy-promote"]').trigger('click')
    await flushPromises()

    const error = wrapper.find('[data-testid="progressive-autonomy-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain(serverDetail)
    expect(wrapper.emitted('changed')).toBeUndefined()
  })
})
