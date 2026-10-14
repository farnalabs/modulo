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

  it('renders the manual approval default when defaultLevel is null', () => {
    // A NULL stored default is the backend's manual_approval default, so the
    // row shows that label — never "Unknown level".
    const wrapper = mountPanel({ defaultLevel: null })

    const defaultRow = wrapper.find('[data-testid="progressive-autonomy-default"]')
    expect(defaultRow.text()).toContain('Manual approval')
    expect(defaultRow.text()).not.toContain('Unknown')
  })

  it('surfaces a network failure that never produced an HTTP response', async () => {
    // A thrown TypeError (fetch-level failure) is not an HTTP problem+json
    // body: the catch path must still land in the alert region.
    apiMocks.post.mockRejectedValue(new TypeError('Failed to fetch'))
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('manual_approval')
    await wrapper.find('[data-testid="progressive-autonomy-demote"]').trigger('click')
    await flushPromises()

    const error = wrapper.find('[data-testid="progressive-autonomy-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('Failed to change the autonomy level: Failed to fetch')
    expect(wrapper.emitted('changed')).toBeUndefined()
    // The displayed earned level was never touched, and the button re-enables.
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Notify on complete',
    )
    expect(
      wrapper.find('[data-testid="progressive-autonomy-demote"]').attributes('disabled'),
    ).toBeUndefined()
  })

  it('disables demote at the floor and exposes the admin/owner hint to AT', async () => {
    const wrapper = mountPanel({ earnedLevel: 'manual_approval' })

    const demote = wrapper.find('[data-testid="progressive-autonomy-demote"]')
    const promote = wrapper.find('[data-testid="progressive-autonomy-promote"]')
    // Already at the ladder floor: there is nothing below to demote to.
    expect(demote.attributes('disabled')).toBeDefined()

    // A legal upward target within the ceiling re-enables promote only.
    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('notify_on_complete')
    expect(promote.attributes('disabled')).toBeUndefined()
    expect(demote.attributes('disabled')).toBeDefined()

    // The permission rule is not hover-only: each button carries it for AT.
    expect(demote.attributes('aria-description')).toContain('organisation admin')
    expect(promote.attributes('aria-description')).toContain('organisation admin')
  })

  it('clears a stale local override only once fresher prop data arrives', async () => {
    apiMocks.post.mockResolvedValue({
      data: successBody('manual_approval', 'notify_on_complete'),
      error: undefined,
      response: { status: 200 },
    })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('manual_approval')
    await wrapper.find('[data-testid="progressive-autonomy-demote"]').trigger('click')
    await flushPromises()
    // Local override: manual_approval, stamped by the response at 2026-09-02.
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Manual approval',
    )

    // A stale in-flight refetch (older timestamp, pre-write value) must not
    // clobber the override the write just established.
    await wrapper.setProps({
      earnedLevel: 'notify_on_complete',
      earnedUpdatedAt: '2026-09-01T10:00:00Z',
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Manual approval',
    )

    // A concurrent writer's fresher write (different value, later timestamp)
    // wins — the override never sticks over it.
    await wrapper.setProps({
      earnedLevel: 'fully_autonomous',
      earnedUpdatedAt: '2026-09-03T09:00:00Z',
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Fully autonomous',
    )
  })

  it('renders an unknown level verbatim and ranks it below the ladder floor', () => {
    const wrapper = mountPanel({ earnedLevel: 'experimental_level', earnedUpdatedAt: null })

    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'experimental_level',
    )
    // Off-ladder levels rank -1: neither direction is selectable from them.
    expect(
      wrapper.find('[data-testid="progressive-autonomy-demote"]').attributes('disabled'),
    ).toBeDefined()
    expect(
      wrapper.find('[data-testid="progressive-autonomy-promote"]').attributes('disabled'),
    ).toBeDefined()
  })

  it('labels a null level as unknown and blanks an absent earned-at timestamp', () => {
    const wrapper = mountPanel({ earnedLevel: 'notify_on_complete', earnedUpdatedAt: null })
    const vm = wrapper.vm as unknown as {
      levelLabel: (level: string | null) => string
      formattedEarnedAt: string
    }

    expect(vm.levelLabel(null)).toContain('Unknown')
    expect(vm.formattedEarnedAt).toBe('')
  })

  it('falls back to manual approval when both the default and ceiling are null', () => {
    const wrapper = mountPanel({ earnedLevel: null, defaultLevel: null, maxLevel: null })

    expect(wrapper.find('[data-testid="progressive-autonomy-default"]').text()).toContain(
      'Manual approval',
    )
    expect(wrapper.find('[data-testid="progressive-autonomy-ceiling"]').text()).toContain(
      'Inherits the default level',
    )
  })

  it('shows the inherited-ceiling label when no max level is stored', () => {
    const wrapper = mountPanel({ maxLevel: null, defaultLevel: 'notify_on_complete' })

    expect(wrapper.find('[data-testid="progressive-autonomy-default"]').text()).toContain(
      'Notify on complete',
    )
    expect(wrapper.find('[data-testid="progressive-autonomy-ceiling"]').text()).toContain(
      'Inherits the default level',
    )
  })

  it('leaves the earned display to the prop data when no local write happened', async () => {
    const wrapper = mountPanel()
    await wrapper.setProps({
      earnedLevel: 'fully_autonomous',
      earnedUpdatedAt: '2026-09-05T00:00:00Z',
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Fully autonomous',
    )
  })

  it('drops the override once the parent refetch confirms our own write', async () => {
    apiMocks.post.mockResolvedValue({
      data: successBody('manual_approval', 'notify_on_complete'),
      error: undefined,
      response: { status: 200 },
    })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('manual_approval')
    await wrapper.find('[data-testid="progressive-autonomy-demote"]').trigger('click')
    await flushPromises()

    await wrapper.setProps({
      earnedLevel: 'manual_approval',
      earnedUpdatedAt: '2026-09-02T11:22:33Z',
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Manual approval',
    )
  })

  it('treats a missing prop timestamp as fresh and drops the override', async () => {
    apiMocks.post.mockResolvedValue({
      data: successBody('manual_approval', 'notify_on_complete'),
      error: undefined,
      response: { status: 200 },
    })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('manual_approval')
    await wrapper.find('[data-testid="progressive-autonomy-demote"]').trigger('click')
    await flushPromises()

    await wrapper.setProps({ earnedLevel: 'fully_autonomous', earnedUpdatedAt: null })
    await flushPromises()
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Fully autonomous',
    )
  })

  it('treats a missing override timestamp as stale and accepts the prop write', async () => {
    apiMocks.post.mockResolvedValue({
      data: { ...successBody('manual_approval', 'notify_on_complete'), updated_at: null },
      error: undefined,
      response: { status: 200 },
    })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('manual_approval')
    await wrapper.find('[data-testid="progressive-autonomy-demote"]').trigger('click')
    await flushPromises()

    await wrapper.setProps({
      earnedLevel: 'fully_autonomous',
      earnedUpdatedAt: '2026-09-06T00:00:00Z',
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Fully autonomous',
    )
  })

  it('ignores a second apply while one is already pending', async () => {
    let resolvePost: (value: unknown) => void = () => {}
    apiMocks.post.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePost = resolve
        }),
    )
    const wrapper = mountPanel()
    const vm = wrapper.vm as unknown as { apply: (direction: 'demote' | 'promote') => Promise<void> }

    const first = vm.apply('demote')
    await vm.apply('promote')

    expect(apiMocks.post).toHaveBeenCalledTimes(1)

    resolvePost({
      data: successBody('manual_approval', 'notify_on_complete'),
      error: undefined,
      response: { status: 200 },
    })
    await first
    await flushPromises()
  })

  it('derives the status from a problem-detail error that carried no response', async () => {
    apiMocks.post.mockResolvedValue({
      error: {
        type: 'urn:problem:modulo:validation_error',
        title: 'Validation Error',
        status: 422,
        detail: 'promotion refused',
      },
      response: undefined,
    })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('fully_autonomous')
    await wrapper.find('[data-testid="progressive-autonomy-promote"]').trigger('click')
    await flushPromises()

    const error = wrapper.find('[data-testid="progressive-autonomy-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('promotion refused')
  })

  it('falls back to status zero for a non-problem error with no response', async () => {
    apiMocks.post.mockResolvedValue({ error: { detail: 'plain failure' }, response: undefined })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('fully_autonomous')
    await wrapper.find('[data-testid="progressive-autonomy-promote"]').trigger('click')
    await flushPromises()

    const error = wrapper.find('[data-testid="progressive-autonomy-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('plain failure')
  })

  it('falls back to the target level when the success body is empty', async () => {
    apiMocks.post.mockResolvedValue({ data: null, error: undefined, response: { status: 200 } })
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="progressive-autonomy-target"]').setValue('fully_autonomous')
    await wrapper.find('[data-testid="progressive-autonomy-promote"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="progressive-autonomy-earned"]').text()).toContain(
      'Fully autonomous',
    )
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })
})
