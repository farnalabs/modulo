import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import AssistantOnlyView from '../views/AssistantOnlyView.vue'
import { useAssistantStore } from '../composables/useAssistantStore'
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

vi.mock('@/components/assistant/AssistantChat.vue', () => ({
  default: { name: 'AssistantChat', template: '<div data-testid="assistant-chat-stub" />' },
}))
vi.mock('@/components/assistant/AssistantSkillManager.vue', () => ({
  default: { name: 'AssistantSkillManager', template: '<div />' },
}))
vi.mock('@/components/assistant/AssistantContextSources.vue', () => ({
  default: { name: 'AssistantContextSources', template: '<div />' },
}))
vi.mock('@/components/assistant/AssistantSessionDrawer.vue', () => ({
  default: { name: 'AssistantSessionDrawer', template: '<div />' },
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

describe('AssistantOnlyView', () => {
  it('shows the unavailable state when dev mode is off', async () => {
    ;(usePlanStore as any).mockReturnValueOnce({
      devMode: false,
      loaded: true,
      features: {},
      fetchPlan: vi.fn(() => Promise.resolve()),
      featureEnabled: vi.fn(() => true),
    })
    const wrapper = mount(AssistantOnlyView)
    await flushPromises()
    expect(wrapper.find('[data-testid="assistant-only-unavailable"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="assistant-only-view"]').exists()).toBe(true)
  })

  it('empty sessions and no active session — does NOT auto-create on mount', async () => {
    const store = useAssistantStore()
    const createSpy = vi.fn().mockResolvedValue(null)
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    store.createSession = createSpy as never
    store.loadSession = loadSpy as never

    const wrapper = mount(AssistantOnlyView)
    await flushPromises()

    expect(createSpy).not.toHaveBeenCalled()
    expect(loadSpy).not.toHaveBeenCalled()
    // vitest v4 no longer flushes Vue's nextTick DOM patch via flushPromises()
    // alone; wait for the post-mount reactive render to settle.
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="assistant-only-empty"]').exists()).toBe(true)
    })
  })

  it('restored activeSessionId present in sessions → loadSession called on mount', async () => {
    localStorage.setItem('assistant-active-session', 'session-1')
    const store = useAssistantStore()
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    store.loadSession = loadSpy as never
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [makeSession('session-1', 'Alpha')], total: 1 } })

    const wrapper = mount(AssistantOnlyView)
    await flushPromises()

    await vi.waitFor(() => {
      expect(loadSpy).toHaveBeenCalledWith('session-1')
      expect(wrapper.find('[data-testid="assistant-only-tab-bar"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="assistant-only-chat"]').exists()).toBe(true)
    })
  })

  it('renders resolved i18n keys for the assistant-only chrome (banner is not a raw key)', async () => {
    localStorage.setItem('assistant-active-session', 'session-1')
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [makeSession('session-1', 'Alpha')], total: 1 } })

    const wrapper = mount(AssistantOnlyView)
    await flushPromises()

    const banner = wrapper.find('[data-testid="assistant-only-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).not.toContain('components.assistant.AssistantOnlyView.banner')
    expect(banner.text()).toContain('chat-only view')
  })
})
