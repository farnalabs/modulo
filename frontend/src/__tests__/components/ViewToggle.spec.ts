import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const mockViews = [
  { id: 'view-1', name: 'Production Overview' },
  { id: 'view-2', name: 'Staging Dashboard' },
]

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn() },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
}))

import ViewToggle from '../../components/ViewToggle.vue'
import { usePlanStore } from '../../stores/planStore'

describe('ViewToggle', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders view list from API', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { views: mockViews }, error: null })

    const store = usePlanStore()
    store.$patch({ features: { saved_views: true } })

    const wrapper = mount(ViewToggle)
    await nextTick()
    await nextTick()

    expect(api.GET).toHaveBeenCalledWith('/api/v1/views')

    const trigger = wrapper.find('[data-testid="view-toggle-trigger"]')
    expect(trigger.exists()).toBe(true)
    expect(trigger.text()).toContain('Select a saved view...')
  })

  it('toggle emits view-changed event', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { views: mockViews }, error: null })

    const store = usePlanStore()
    store.$patch({ features: { saved_views: true } })

    const wrapper = mount(ViewToggle)
    await nextTick()
    await nextTick()

    const vm = wrapper.vm as any
    vm.selectedViewId = 'view-1'
    await nextTick()

    const toggleSwitch = wrapper.find('[data-testid="view-toggle-switch"]')
    expect(toggleSwitch.exists()).toBe(true)

    await toggleSwitch.trigger('click')

    expect(wrapper.emitted('view-changed')).toBeTruthy()
    expect(wrapper.emitted('view-changed')![0]).toEqual([{ viewId: 'view-1', enabled: true }])
  })

  it('shows active state after toggle', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { views: mockViews }, error: null })

    const store = usePlanStore()
    store.$patch({ features: { saved_views: true } })

    const wrapper = mount(ViewToggle)
    await nextTick()
    await nextTick()

    const vm = wrapper.vm as any
    vm.selectedViewId = 'view-1'
    await nextTick()

    const badge = wrapper.find('[data-testid="view-toggle-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('Inactive')

    await wrapper.find('[data-testid="view-toggle-switch"]').trigger('click')
    await nextTick()

    expect(badge.text()).toContain('Active')
    expect(wrapper.emitted('view-changed')).toBeTruthy()
  })

  it('does not fetch views when feature is disabled', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { views: mockViews }, error: null })

    const store = usePlanStore()
    store.$patch({ features: { saved_views: false } })

    mount(ViewToggle)
    await nextTick()
    await nextTick()

    expect(api.GET).not.toHaveBeenCalled()
  })
})
