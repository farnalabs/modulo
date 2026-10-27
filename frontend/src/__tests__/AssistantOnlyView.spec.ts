import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { setActivePinia, createPinia } from 'pinia'
import AssistantOnlyView from '../views/AssistantOnlyView.vue'
import { useAssistantStore } from '../composables/useAssistantStore'
import { useAssistantTabsStore } from '../composables/useAssistantTabsStore'
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
  default: {
    name: 'AssistantChat',
    methods: { disconnect: vi.fn() },
    template: '<div data-testid="assistant-chat-stub" />',
  },
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

  it('resumes a tab when the active session changes to a live session with no open tab', async () => {
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [], total: 0 } })
    const store = useAssistantStore()
    const tabsStore = useAssistantTabsStore()

    const wrapper = mount(AssistantOnlyView)
    await flushPromises()

    store.sessions = [makeSession('session-live', 'Live')] as never
    await nextTick()
    const resumeSpy = vi.spyOn(tabsStore, 'resumeTab').mockResolvedValue(undefined)
    store.activeSessionId = 'session-live'

    await vi.waitFor(() => {
      expect(resumeSpy).toHaveBeenCalledWith('session-live')
    })
    wrapper.unmount()
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

  it('returns home from the header button', async () => {
    const router = await import('vue-router')
    const wrapper = mount(AssistantOnlyView)
    await flushPromises()

    await wrapper.find('[data-testid="assistant-only-return-home"]').trigger('click')
    expect(router.useRouter().push).toHaveBeenCalledWith('/')
  })

  it('starts a fresh tab and resets the selected sub-tab', async () => {
    const tabsStore = useAssistantTabsStore()
    const addTabSpy = vi.spyOn(tabsStore, 'addTab').mockResolvedValue(makeSession('session-new', 'New') as never)
    const wrapper = mount(AssistantOnlyView)
    await flushPromises()

    await wrapper.find('[data-testid="assistant-only-new-tab"]').trigger('click')
    expect(addTabSpy).toHaveBeenCalledOnce()
    expect((wrapper.vm as unknown as { subTab: string }).subTab).toBe('chat')
  })

  it('switches and closes session tabs while disconnecting the chat', async () => {
    const store = useAssistantStore()
    store.sessions = [makeSession('session-1', 'Alpha'), makeSession('session-2', 'Beta')] as never
    store.activeSessionId = 'session-1'
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [makeSession('session-1', 'Alpha'), makeSession('session-2', 'Beta')], total: 2 } })
    localStorage.setItem('assistant-active-session', 'session-1')
    const tabsStore = useAssistantTabsStore()
    tabsStore.tabs.push(
      { tabId: 'tab-1', sessionId: 'session-1' },
      { tabId: 'tab-2', sessionId: 'session-2' },
    )
    const resumeSpy = vi.spyOn(tabsStore, 'resumeTab').mockResolvedValue(undefined)
    const closeSpy = vi.spyOn(tabsStore, 'closeTab')
    const wrapper = mount(AssistantOnlyView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="assistant-only-tab-tab-2"]').trigger('click')
    expect(resumeSpy).toHaveBeenCalledWith('session-2')
    await wrapper.find('[data-testid="assistant-only-tab-close-tab-2"]').trigger('click')
    expect(closeSpy).toHaveBeenCalledWith('tab-2')
  })

  it('changes sub-tabs and supports keyboard navigation', async () => {
    localStorage.setItem('assistant-active-session', 'session-1')
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [makeSession('session-1', 'Alpha')], total: 1 } })
    const wrapper = mount(AssistantOnlyView)
    await flushPromises()

    await wrapper.find('[data-testid="assistant-only-subtab-skills"]').trigger('click')
    expect((wrapper.vm as unknown as { subTab: string }).subTab).toBe('skills')
    await wrapper.find('[data-testid="assistant-only-subtab-skills"]').trigger('keydown', { key: 'ArrowRight' })
    expect((wrapper.vm as unknown as { subTab: string }).subTab).toBe('sources')
    await wrapper.find('[data-testid="assistant-only-subtab-sources"]').trigger('keydown', { key: 'ArrowLeft' })
    expect((wrapper.vm as unknown as { subTab: string }).subTab).toBe('skills')
    await wrapper.find('[data-testid="assistant-only-subtab-skills"]').trigger('keydown', { key: 'End' })
    expect((wrapper.vm as unknown as { subTab: string }).subTab).toBe('sessions')
    await wrapper.find('[data-testid="assistant-only-subtab-sessions"]').trigger('keydown', { key: 'Home' })
    expect((wrapper.vm as unknown as { subTab: string }).subTab).toBe('chat')
  })

  it('supports session tab keyboard navigation and handles session drawer selection', async () => {
    localStorage.setItem('assistant-active-session', 'session-1')
    ;(api.GET as any).mockResolvedValue({
      error: null,
      data: { items: [makeSession('session-1', 'Alpha'), makeSession('session-2', 'Beta')], total: 2 },
    })
    const tabsStore = useAssistantTabsStore()
    tabsStore.tabs.push(
      { tabId: 'tab-1', sessionId: 'session-1' },
      { tabId: 'tab-2', sessionId: 'session-2' },
    )
    const wrapper = mount(AssistantOnlyView)
    await flushPromises()
    await nextTick()

    const tab1 = wrapper.find('[data-testid="assistant-only-tab-tab-1"]')
    await tab1.trigger('keydown', { key: 'ArrowRight' })
    expect((wrapper.vm as unknown as { subTab: string }).subTab).toBe('chat')
    const tab2 = wrapper.find('[data-testid="assistant-only-tab-tab-2"]')
    await tab2.trigger('keydown', { key: 'ArrowLeft' })
    await tab1.trigger('keydown', { key: 'End' })
    await tab2.trigger('keydown', { key: 'Home' })

    const drawer = wrapper.findComponent({ name: 'AssistantSessionDrawer' })
    drawer.vm.$emit('select-session')
    await nextTick()
    expect((wrapper.vm as unknown as { subTab: string }).subTab).toBe('chat')
  })

  it('handles clicking the already active tab to switch to the chat view', async () => {
    localStorage.setItem('assistant-active-session', 'session-1')
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [makeSession('session-1', 'Alpha')], total: 1 } })
    const tabsStore = useAssistantTabsStore()
    tabsStore.tabs.push({ tabId: 'tab-1', sessionId: 'session-1' })
    const wrapper = mount(AssistantOnlyView)
    await flushPromises()

    const vm = wrapper.vm as unknown as { subTab: string }
    vm.subTab = 'sources'
    await wrapper.find('[data-testid="assistant-only-tab-tab-1"]').trigger('click')
    expect(vm.subTab).toBe('chat')
  })

  it('fetches plan when planStore is not loaded yet', async () => {
    const fetchPlanMock = vi.fn().mockResolvedValue(undefined)
    ;(usePlanStore as any).mockReturnValueOnce({
      devMode: true,
      loaded: false,
      features: {},
      fetchPlan: fetchPlanMock,
      featureEnabled: vi.fn(() => true),
    })
    mount(AssistantOnlyView)
    await flushPromises()

    expect(fetchPlanMock).toHaveBeenCalled()
  })

  it('renders a streaming indicator on the active tab', async () => {
    localStorage.setItem('assistant-active-session', 'session-1')
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [makeSession('session-1', 'Alpha')], total: 1 } })
    const store = useAssistantStore()
    store.isStreaming = true
    const tabsStore = useAssistantTabsStore()
    tabsStore.tabs.push({ tabId: 'tab-1', sessionId: 'session-1' })
    const wrapper = mount(AssistantOnlyView)
    await flushPromises()

    expect(wrapper.find('.assistant-live-dot').exists()).toBe(true)
  })

  it('renders a session number or short id when a session has no name', async () => {
    const store = useAssistantStore()
    store.sessions = [makeSession('abcdefghijk', '')] as never
    store.activeSessionId = 'abcdefghijk'
    ;(api.GET as any).mockResolvedValue({ error: null, data: { items: [makeSession('abcdefghijk', '')], total: 1 } })
    localStorage.setItem('assistant-active-session', 'abcdefghijk')
    const tabsStore = useAssistantTabsStore()
    tabsStore.tabs.push({ tabId: 'tab-1', sessionId: 'abcdefghijk' })
    const wrapper = mount(AssistantOnlyView)
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="assistant-only-tab-tab-1"]').text()).toContain('Session #1')
  })
})
