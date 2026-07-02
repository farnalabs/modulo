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

  it('fetches view list on mount when feature is enabled', async () => {
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

  it('clicking toggle emits view-changed with correct payload', async () => {
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

    await wrapper.find('[data-testid="view-toggle-switch"]').trigger('click')

    expect(wrapper.emitted('view-changed')).toBeTruthy()
    expect(wrapper.emitted('view-changed')![0]).toEqual([{ viewId: 'view-1', enabled: true }])
  })

  it('shows Active badge when a view is selected and toggle is on', async () => {
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
  })

  it('shows lock icon when feature is disabled via FeatureGate', async () => {
    const store = usePlanStore()
    store.$patch({ features: { saved_views: false } })

    const wrapper = mount(ViewToggle)
    await nextTick()
    await nextTick()

    const lock = wrapper.find('[data-testid="feature-gate-lock"]')
    expect(lock.exists()).toBe(true)
  })

  it('does not fetch views from API when feature is disabled', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { views: mockViews }, error: null })

    const store = usePlanStore()
    store.$patch({ features: { saved_views: false } })

    mount(ViewToggle)
    await nextTick()
    await nextTick()

    expect(api.GET).not.toHaveBeenCalled()
  })

  it('selecting a view from the dropdown emits view-changed and sets selectedViewId', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { views: mockViews }, error: null })

    const store = usePlanStore()
    store.$patch({ features: { saved_views: true } })

    const wrapper = mount(ViewToggle)
    await nextTick()
    await nextTick()

    const vm = wrapper.vm as any
    expect(vm.views).toEqual(mockViews)

    await wrapper.find('[data-testid="view-toggle-trigger"]').trigger('click')
    await nextTick()

    const items = document.body.querySelectorAll('[data-testid="view-toggle-item"]')
    if (items.length > 0) {
      (items[0] as HTMLElement).click()
    } else {
      const select = wrapper.findComponent({ name: 'Select' })
      await (select as any).vm.$emit('update:model-value', 'view-1')
    }
    await nextTick()
    await nextTick()

    expect(wrapper.emitted('view-changed')).toBeTruthy()
    expect(wrapper.emitted('view-changed')![0]).toEqual([{ viewId: 'view-1', enabled: false }])
    expect(vm.selectedViewId).toBe('view-1')
  })
})
