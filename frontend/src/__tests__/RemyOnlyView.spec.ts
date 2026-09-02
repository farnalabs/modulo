import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import RemyOnlyView from '../views/RemyOnlyView.vue'
import { useRemyStore } from '../composables/useRemyStore'
import { api } from '@/lib/api/client'
import { usePlanStore } from '@/stores/planStore'

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
  api: {
    POST: vi.fn(() => Promise.resolve({ error: null, data: { id: 'session-9', session_number: 1 } })),
    GET: vi.fn(() => Promise.resolve({ error: null, data: { items: [] } })),
    PATCH: vi.fn(() => Promise.resolve({ error: null, data: {} })),
    DELETE: vi.fn(() => Promise.resolve({ error: null, data: {} })),
  },
}))

vi.mock('@/stores/planStore', () => ({
  usePlanStore: vi.fn(() => ({
    devMode: true,
    loaded: true,
    features: {},
    fetchPlan: vi.fn(() => Promise.resolve()),
    featureEnabled: vi.fn(() => true),
  })),
}))

vi.mock('@/composables/useUiCommandExecutor', () => ({
  pauseUiCommands: vi.fn(),
  resumeUiCommands: vi.fn(),
  abortUiCommands: vi.fn(),
  executeCommandBatch: vi.fn(),
  isPaused: vi.fn(() => false),
}))

vi.mock('@/components/remy/RemyChat.vue', () => ({
  default: { name: 'RemyChat', template: '<div data-testid="remy-chat-stub" />' },
}))
vi.mock('@/components/remy/RemySkillManager.vue', () => ({
  default: { name: 'RemySkillManager', template: '<div />' },
}))
vi.mock('@/components/remy/RemyContextSources.vue', () => ({
  default: { name: 'RemyContextSources', template: '<div />' },
}))
vi.mock('@/components/remy/RemySessionDrawer.vue', () => ({
  default: { name: 'RemySessionDrawer', template: '<div />' },
}))

function makeSession(id: string, name: string) {
  return {
    id,
    user_id: 'user-1',
    name,
    session_number: 1,
    provider: 'anthropic',
    model: 'claude-sonnet-4-20250514',
    context_window_tokens: 200000,
    system_prompt_hash: null,
    message_count: 0,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  vi.restoreAllMocks()
})

describe('RemyOnlyView', () => {
  it('shows the unavailable state when dev mode is off', async () => {
    ;(usePlanStore as any).mockReturnValueOnce({
      devMode: false,
      loaded: true,
      features: {},
      fetchPlan: vi.fn(() => Promise.resolve()),
      featureEnabled: vi.fn(() => true),
    })
    const wrapper = mount(RemyOnlyView)
    await flushPromises()
    expect(wrapper.find('[data-testid="remy-only-unavailable"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="remy-only-view"]').exists()).toBe(true)
  })

  it('empty sessions and no active session — does NOT auto-create on mount', async () => {
    const store = useRemyStore()
    const createSpy = vi.fn().mockResolvedValue(null)
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    store.createSession = createSpy as never
    store.loadSession = loadSpy as never

    const wrapper = mount(RemyOnlyView)
    await flushPromises()

    expect(createSpy).not.toHaveBeenCalled()
    expect(loadSpy).not.toHaveBeenCalled()
    // vitest v4 no longer flushes Vue's nextTick DOM patch via flushPromises()
    // alone; wait for the post-mount reactive render to settle.
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="remy-only-empty"]').exists()).toBe(true)
    })
  })

  it('restored activeSessionId present in sessions → loadSession called on mount', async () => {
    localStorage.setItem('remy-active-session', 'session-1')
    const store = useRemyStore()
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    store.loadSession = loadSpy as never
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [makeSession('session-1', 'Alpha')], total: 1 } })

    const wrapper = mount(RemyOnlyView)
    await flushPromises()

    await vi.waitFor(() => {
      expect(loadSpy).toHaveBeenCalledWith('session-1')
      expect(wrapper.find('[data-testid="remy-only-tab-bar"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="remy-only-chat"]').exists()).toBe(true)
    })
  })

  it('renders resolved i18n keys for the remy-only chrome (banner is not a raw key)', async () => {
    localStorage.setItem('remy-active-session', 'session-1')
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [makeSession('session-1', 'Alpha')], total: 1 } })

    const wrapper = mount(RemyOnlyView)
    await flushPromises()

    const banner = wrapper.find('[data-testid="remy-only-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).not.toContain('components.remy.RemyOnlyView.banner')
    expect(banner.text()).toContain('chat-only view')
  })
})
