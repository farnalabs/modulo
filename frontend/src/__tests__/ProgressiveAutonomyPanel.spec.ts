// FAR-1696 slice (b): progressive-autonomy visibility panel on the pipeline
// editor. The panel reads the pipeline's default / ceiling / earned autonomy
// levels and drives manual promote/demote over the live REST endpoints.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

vi.mock('../lib/api/client', () => ({
  api: {
    POST: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import ProgressiveAutonomyPanel from '../components/pipeline/ProgressiveAutonomyPanel.vue'

const PANEL = '[data-testid="progressive-autonomy-panel"]'
const EARNED = '[data-testid="progressive-autonomy-earned"]'
const DEMOTE = '[data-testid="progressive-autonomy-demote"]'
const PROMOTE = '[data-testid="progressive-autonomy-promote"]'
const REASON = '[data-testid="progressive-autonomy-reason"]'
const STATUS = '[data-testid="progressive-autonomy-status"]'
const ERROR = '[data-testid="progressive-autonomy-error"]'

function mountPanel(props: Record<string, unknown> = {}) {
  return mount(ProgressiveAutonomyPanel, {
    props: {
      pipelineId: 'pipeline-1',
      defaultLevel: 'manual_approval',
      maxLevel: null,
      earnedLevel: null,
      earnedUpdatedAt: null,
      ...props,
    },
  })
}

function buttonDisabled(wrapper: ReturnType<typeof mount>, selector: string): boolean {
  return (wrapper.find(selector).element as HTMLButtonElement).disabled
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.POST).mockResolvedValue({ data: {}, error: undefined } as never)
})

describe('ProgressiveAutonomyPanel', () => {
  it('renders the pipeline default, ceiling and earned levels', () => {
    const wrapper = mountPanel({
      defaultLevel: 'manual_approval',
      maxLevel: 'fully_autonomous',
      earnedLevel: 'notify_on_complete',
      earnedUpdatedAt: '2026-10-13T10:00:00Z',
    })

    const panelText = wrapper.find(PANEL).text()
    expect(panelText).toContain('Manual approval')
    expect(panelText).toContain('Fully autonomous')
    expect(wrapper.find(EARNED).text()).toBe('Notify on complete')
    wrapper.unmount()
  })

  it('renders an explicit not-set fallback when the earned level is null', () => {
    const wrapper = mountPanel({ defaultLevel: 'notify_on_complete', earnedLevel: null })

    expect(wrapper.find(EARNED).text()).toContain('Not set')
    expect(wrapper.find(EARNED).text()).toContain('Notify on complete')
    wrapper.unmount()
  })

  it('demotes one rung with the optional reason and emits changed on success', async () => {
    vi.mocked(api.POST).mockResolvedValueOnce({
      data: { earned_autonomy_level: 'manual_approval' },
      error: undefined,
    } as never)
    const wrapper = mountPanel({ earnedLevel: 'notify_on_complete', maxLevel: 'fully_autonomous' })

    await wrapper.find(REASON).setValue('  too risky  ')
    await wrapper.find(DEMOTE).trigger('click')
    await flushPromises()

    expect(api.POST).toHaveBeenCalledWith(
      '/api/v1/pipelines/{pipeline_id}/autonomy/demote',
      expect.objectContaining({
        params: { path: { pipeline_id: 'pipeline-1' } },
        body: { level: 'manual_approval', reason: 'too risky' },
      }),
    )
    expect(wrapper.find(STATUS).text()).toContain('Manual approval')
    expect(wrapper.emitted('changed')).toHaveLength(1)
    wrapper.unmount()
  })

  it('promotes one rung and omits the reason when none is entered', async () => {
    vi.mocked(api.POST).mockResolvedValueOnce({
      data: { earned_autonomy_level: 'fully_autonomous' },
      error: undefined,
    } as never)
    const wrapper = mountPanel({ earnedLevel: 'notify_on_complete', maxLevel: 'fully_autonomous' })

    await wrapper.find(PROMOTE).trigger('click')
    await flushPromises()

    expect(api.POST).toHaveBeenCalledWith(
      '/api/v1/pipelines/{pipeline_id}/autonomy/promote',
      expect.objectContaining({
        params: { path: { pipeline_id: 'pipeline-1' } },
        body: { level: 'fully_autonomous' },
      }),
    )
    expect(wrapper.emitted('changed')).toHaveLength(1)
    wrapper.unmount()
  })

  it('disables demote at the floor and promote at the ceiling', () => {
    const floor = mountPanel({ earnedLevel: 'manual_approval', maxLevel: 'fully_autonomous' })
    expect(buttonDisabled(floor, DEMOTE)).toBe(true)
    floor.unmount()

    const ceiling = mountPanel({ earnedLevel: 'fully_autonomous', maxLevel: 'fully_autonomous' })
    expect(buttonDisabled(ceiling, PROMOTE)).toBe(true)
    ceiling.unmount()
  })

  it('prevents a promote that would exceed the ceiling', () => {
    // Earned notify_on_complete, ceiling notify_on_complete -> the next rung
    // (fully_autonomous) is above the ceiling, so promote is disabled.
    const wrapper = mountPanel({ earnedLevel: 'notify_on_complete', maxLevel: 'notify_on_complete' })
    expect(buttonDisabled(wrapper, PROMOTE)).toBe(true)
    wrapper.unmount()
  })

  it('surfaces a 403 as a not-permitted error and does not emit changed', async () => {
    vi.mocked(api.POST).mockResolvedValueOnce({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:forbidden',
        title: 'Forbidden',
        status: 403,
        detail: 'admin or pipeline owner required',
      },
      response: { status: 403 },
    } as never)
    const wrapper = mountPanel({ earnedLevel: 'notify_on_complete', maxLevel: 'fully_autonomous' })

    await wrapper.find(PROMOTE).trigger('click')
    await flushPromises()

    expect(wrapper.find(ERROR).text()).toContain('admin or pipeline owner required')
    expect(wrapper.emitted('changed')).toBeUndefined()
    wrapper.unmount()
  })

  it('surfaces a 422 with the server detail', async () => {
    vi.mocked(api.POST).mockResolvedValueOnce({
      data: undefined,
      error: {
        type: 'urn:problem:modulo:validation_error',
        title: 'Validation Error',
        status: 422,
        detail: 'Promotion must raise autonomy above the current level',
      },
      response: { status: 422 },
    } as never)
    const wrapper = mountPanel({ earnedLevel: 'manual_approval', maxLevel: 'fully_autonomous' })

    await wrapper.find(PROMOTE).trigger('click')
    await flushPromises()

    expect(wrapper.find(ERROR).text()).toContain('Promotion must raise autonomy above the current level')
    wrapper.unmount()
  })

  it('surfaces a thrown transport failure', async () => {
    vi.mocked(api.POST).mockRejectedValueOnce(new Error('network down'))
    const wrapper = mountPanel({ earnedLevel: 'manual_approval', maxLevel: 'fully_autonomous' })

    await wrapper.find(PROMOTE).trigger('click')
    await flushPromises()

    expect(wrapper.find(ERROR).text()).toContain('network down')
    wrapper.unmount()
  })

  it('falls back to the pinned default when no levels are set', () => {
    const wrapper = mountPanel({ defaultLevel: null, maxLevel: null, earnedLevel: null })

    expect(wrapper.find(PANEL).text()).toContain('Manual approval')
    expect(wrapper.find(EARNED).text()).toContain('Not set')
    wrapper.unmount()
  })

  it('hides the updated-at stamp when the earned level carries no timestamp', () => {
    const wrapper = mountPanel({ earnedLevel: 'notify_on_complete', earnedUpdatedAt: null })

    expect(wrapper.find(EARNED).text()).toBe('Notify on complete')
    expect(wrapper.find(PANEL).text()).not.toContain('updated')
    wrapper.unmount()
  })

  it('falls back to the target level when the success response omits the earned level', async () => {
    vi.mocked(api.POST).mockResolvedValueOnce({ data: {}, error: undefined } as never)
    const wrapper = mountPanel({ earnedLevel: 'manual_approval', maxLevel: 'fully_autonomous' })

    await wrapper.find(PROMOTE).trigger('click')
    await flushPromises()

    expect(wrapper.find(STATUS).text()).toContain('Notify on complete')
    wrapper.unmount()
  })

  it('surfaces a generic failure for a non-403/422 error', async () => {
    vi.mocked(api.POST).mockResolvedValueOnce({
      data: undefined,
      error: { type: 'about:blank', title: 'Server Error', status: 500, detail: 'upstream boom' },
      response: { status: 500 },
    } as never)
    const wrapper = mountPanel({ earnedLevel: 'manual_approval', maxLevel: 'fully_autonomous' })

    await wrapper.find(PROMOTE).trigger('click')
    await flushPromises()

    expect(wrapper.find(ERROR).text()).toContain('Failed to change autonomy')
    wrapper.unmount()
  })

  it('ignores a second change while one is already in flight', async () => {
    let release: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => {
      release = resolve
    })
    vi.mocked(api.POST).mockReturnValueOnce(pending as never)
    const wrapper = mountPanel({ earnedLevel: 'manual_approval', maxLevel: 'fully_autonomous' })

    void wrapper.find(PROMOTE).trigger('click')
    void wrapper.find(PROMOTE).trigger('click')
    release({ data: {}, error: undefined })
    await flushPromises()

    expect(api.POST).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
