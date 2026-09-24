import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { setActivePinia, createPinia } from 'pinia'
import type { ChatMessage, ChatSession } from '../../types/assistant'

const { apiGet, apiPost, apiPatch, apiDelete, featureEnabledMock } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  featureEnabledMock: vi.fn(),
}))

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
  api: {
    GET: apiGet,
    POST: apiPost,
    PATCH: apiPatch,
    PUT: vi.fn(),
    DELETE: apiDelete,
  },
}))

vi.mock('@/composables/useUiCommandExecutor', () => ({
  setActionSpeed: vi.fn(),
  resumeUiCommands: vi.fn(),
  pauseUiCommands: vi.fn(),
  abortUiCommands: vi.fn(),
  executeCommandBatch: vi.fn(),
  isPaused: vi.fn(() => false),
}))

vi.mock('@/stores/planStore', () => ({
  usePlanStore: vi.fn(() => ({
    featureEnabled: featureEnabledMock,
  })),
}))

vi.mock('../../components/assistant/AssistantChat.vue', () => ({
  default: { name: 'AssistantChat', template: '<div data-testid="assistant-chat-stub" />' },
}))
vi.mock('../../components/assistant/AssistantSessionDrawer.vue', () => ({
  default: {
    name: 'AssistantSessionDrawer',
    emits: ['close', 'select-session'],
    template: '<div data-testid="assistant-session-drawer-stub" />',
  },
}))
vi.mock('../../components/assistant/AssistantSkillManager.vue', () => ({
  default: { name: 'AssistantSkillManager', template: '<div data-testid="assistant-skill-manager-stub" />' },
}))
vi.mock('../../components/assistant/AssistantContextSources.vue', () => ({
  default: { name: 'AssistantContextSources', template: '<div data-testid="assistant-context-sources-stub" />' },
}))

import AssistantPanel from '../../components/assistant/AssistantPanel.vue'
import { useAssistantStore } from '../../composables/useAssistantStore'

function makeSession(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    id: 'sess-1',
    user_id: 'user-1',
    name: 'My Session',
    session_number: 3,
    provider: 'openai',
    model: 'gpt-4',
    context_window_tokens: 200000,
    system_prompt_hash: null,
    message_count: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'msg-1',
    session_id: 'sess-1',
    role: 'assistant',
    content: 'Hello there',
    tool_calls_json: null,
    tool_results_json: null,
    token_count: null,
    parent_id: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

async function mountPanel(): Promise<ReturnType<typeof mount>> {
  const wrapper = mount(AssistantPanel)
  await flushPromises()
  return wrapper
}

/** Mount the panel in an open state with a session pre-seeded.
 *  Tests that need to interact with panel internals (titlebar, tabs, etc.)
 *  must use this helper — the default panelState is 'closed', so the panel
 *  does not render without explicit setup. */
async function mountPanelOpen(
  state: 'floating' | 'docked' = 'floating',
  sessionOverrides: Partial<ChatSession> = {},
): Promise<ReturnType<typeof mount>> {
  const store = useAssistantStore()
  store.panelState = state
  store.activeSessionId = 'sess-1'
  const session = makeSession(sessionOverrides)
  // Override API mock so fetchSessions returns our session
  apiGet.mockImplementation((path: string) => {
    if (path === '/api/v1/assistant/sessions') {
      return Promise.resolve({ data: { items: [session] }, error: undefined })
    }
    if (path === '/api/v1/assistant/sessions/{session_id}/messages') {
      return Promise.resolve({ data: { items: [] }, error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  })
  const wrapper = mount(AssistantPanel)
  await flushPromises()
  return wrapper
}

describe('AssistantPanel', () => {
  let createObjectURLSpy: ReturnType<typeof vi.fn>
  let revokeObjectURLSpy: ReturnType<typeof vi.fn>

  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.clearAllMocks()
    featureEnabledMock.mockReturnValue(false)
    apiGet.mockResolvedValue({ data: { items: [] }, error: undefined })
    apiPost.mockResolvedValue({ data: makeSession(), error: undefined })
    apiPatch.mockResolvedValue({ data: makeSession(), error: undefined })
    apiDelete.mockResolvedValue({ data: null, error: undefined })
    createObjectURLSpy = vi.fn(() => 'blob:mock-url')
    revokeObjectURLSpy = vi.fn()
    URL.createObjectURL = createObjectURLSpy as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = revokeObjectURLSpy as unknown as typeof URL.revokeObjectURL
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('renders the floating open button when the panel is closed and opens the panel on click', async () => {
    const store = useAssistantStore()
    store.panelState = 'closed'
    const wrapper = await mountPanel()
    const openBtn = wrapper.find('.assistant-floating-btn')
    expect(openBtn.exists()).toBe(true)
    expect(wrapper.find('.assistant-panel').exists()).toBe(false)
    await openBtn.trigger('click')
    expect(store.panelState).toBe('floating')
    expect(wrapper.find('.assistant-panel').exists()).toBe(true)
  })

  it('defaults to closed state — only the launcher button renders, not the panel overlay', async () => {
    const store = useAssistantStore()
    // Fresh localStorage means the default kicks in (should be 'closed')
    expect(store.panelState).toBe('closed')
    const wrapper = await mountPanel()
    expect(wrapper.find('.assistant-panel').exists()).toBe(false)
    expect(wrapper.find('.assistant-floating-btn').exists()).toBe(true)
  })

  it('renders the docked panel when explicitly set to docked', async () => {
    const store = useAssistantStore()
    store.panelState = 'docked'
    const wrapper = await mountPanel()
    expect(wrapper.find('.assistant-panel').exists()).toBe(true)
    expect(wrapper.find('[data-testid="assistant-chat-stub"]').isVisible()).toBe(true)
    expect(wrapper.find('.assistant-tab.active').text()).toBe('Chat')
  })

  it('switches tabs between chat, skills, sessions and sources', async () => {
    const wrapper = await mountPanelOpen()
    const tabs = wrapper.findAll('.assistant-tab')
    await tabs[1].trigger('click')
    expect(wrapper.find('[data-testid="assistant-skill-manager-stub"]').exists()).toBe(true)
    await tabs[2].trigger('click')
    expect(wrapper.find('[data-testid="assistant-session-drawer-stub"]').isVisible()).toBe(true)
    await tabs[3].trigger('click')
    expect(wrapper.find('[data-testid="assistant-context-sources-stub"]').isVisible()).toBe(true)
    await tabs[0].trigger('click')
    expect(wrapper.find('[data-testid="assistant-chat-stub"]').isVisible()).toBe(true)
  })

  it('docks a floating panel and undocks a docked panel', async () => {
    const wrapper = await mountPanelOpen('floating')
    const store = useAssistantStore()
    expect(store.panelState).toBe('floating')
    const dockBtn = wrapper.find('button[title="Dock"]')
    expect(dockBtn.exists()).toBe(true)
    await dockBtn.trigger('click')
    expect(store.panelState).toBe('docked')
    const undockBtn = wrapper.find('button[title="Undock"]')
    expect(undockBtn.exists()).toBe(true)
    await undockBtn.trigger('click')
    expect(store.panelState).toBe('floating')
  })

  it('maximises and minimises the panel', async () => {
    const store = useAssistantStore()
    store.panelState = 'docked'
    const wrapper = await mountPanel()
    await wrapper.find('button[title="Maximise"]').trigger('click')
    expect(store.panelState).toBe('maximised')
    expect(wrapper.find('button[title="Minimise"]').exists()).toBe(true)
    await wrapper.find('button[title="Minimise"]').trigger('click')
    expect(store.panelState).toBe('docked')
  })

  it('closes the panel from the titlebar close button', async () => {
    const store = useAssistantStore()
    const wrapper = await mountPanelOpen('floating')
    await wrapper.find('button[title="Close"]').trigger('click')
    expect(store.panelState).toBe('closed')
    expect(wrapper.find('.assistant-floating-btn').exists()).toBe(true)
  })

  it('resets session permissions when the shield button is clicked', async () => {
    const store = useAssistantStore()
    const wrapper = await mountPanelOpen()
    await wrapper.find('button[title="Reset Permissions"]').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/v1/assistant/sessions/{session_id}/reset-permissions', {
      params: { path: { session_id: 'sess-1' } },
    })
    expect(store.error).toBeNull()
  })

  it('exports the transcript as a markdown blob', async () => {
    const wrapper = await mountPanelOpen()
    const store = useAssistantStore()
    store.messages = [makeMessage()]
    await nextTick()
    const exportBtn = wrapper.find('button[title="Export Transcript"]')
    expect(exportBtn.exists()).toBe(true)
    await exportBtn.trigger('click')
    expect(createObjectURLSpy).toHaveBeenCalledTimes(1)
    const blob = createObjectURLSpy.mock.calls[0][0] as Blob
    expect(blob.type).toBe('text/markdown;charset=utf-8')
  })

  it('hides the export button when there are no messages', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.find('button[title="Export Transcript"]').exists()).toBe(false)
  })

  it('shows an error banner and dismisses it', async () => {
    const store = useAssistantStore()
    const wrapper = await mountPanelOpen()
    store.error = 'Something broke'
    await nextTick()
    const banner = wrapper.find('.assistant-panel .text-destructive')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Something broke')
    await wrapper.find('button[aria-label="Dismiss error"]').trigger('click')
    expect(store.error).toBeNull()
    await nextTick()
    expect(wrapper.find('.assistant-panel .text-destructive').exists()).toBe(false)
  })

  it('styles rate-limit errors with the warning palette', async () => {
    const store = useAssistantStore()
    const wrapper = await mountPanelOpen()
    store.error = 'Rate limit exceeded, slow down'
    await nextTick()
    const banner = wrapper.find('.text-orange-600')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Rate limit exceeded')
  })

  it('starts renaming from the titlebar and saves via Enter', async () => {
    const wrapper = await mountPanelOpen()
    const store = useAssistantStore()
    expect(store.activeSession).not.toBeNull()
    await wrapper.find('.assistant-titlebar button.text-sm').trigger('click')
    const input = wrapper.find('#assistantpanel-name-input')
    expect(input.exists()).toBe(true)
    expect((input.element as HTMLInputElement).value).toBe('My Session')
    await input.setValue('Renamed Session')
    await input.trigger('keydown.enter')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledWith('/api/v1/assistant/sessions/{session_id}', {
      params: { path: { session_id: 'sess-1' } },
      body: { name: 'Renamed Session' },
    })
  })

  it('does not rename when the edited name is blank', async () => {
    const wrapper = await mountPanelOpen()
    await wrapper.find('.assistant-titlebar button.text-sm').trigger('click')
    const input = wrapper.find('#assistantpanel-name-input')
    expect(input.exists()).toBe(true)
    await input.setValue('   ')
    await input.trigger('keydown.enter')
    await flushPromises()
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('opens the rename input when the store requests a rename', async () => {
    const wrapper = await mountPanelOpen()
    const store = useAssistantStore()
    expect(wrapper.find('#assistantpanel-name-input').exists()).toBe(false)
    store.triggerRename()
    await nextTick()
    expect(wrapper.find('#assistantpanel-name-input').exists()).toBe(true)
  })

  it('falls back to the session number in the titlebar label', async () => {
    const wrapper = await mountPanelOpen('floating', { name: null, session_number: 7 })
    expect(wrapper.find('.assistant-titlebar button.text-sm').text()).toContain('#7')
  })

  it('nudges the panel with arrow keys while floating', async () => {
    const store = useAssistantStore()
    store.panelState = 'floating'
    const wrapper = await mountPanel()
    const beforeX = store.panelPosition.x
    await wrapper.find('.assistant-titlebar').trigger('keydown.right')
    expect(store.panelPosition.x).toBe(beforeX + 1)
    await wrapper.find('.assistant-titlebar').trigger('keydown.down')
    expect(store.panelPosition.y).toBe(81)
  })

  it('drags a floating panel by the titlebar', async () => {
    const store = useAssistantStore()
    store.panelState = 'floating'
    const wrapper = await mountPanel()
    const startX = store.panelPosition.x
    await wrapper.find('.assistant-titlebar').trigger('mousedown', { clientX: 100, clientY: 100 })
    document.dispatchEvent(new MouseEvent('mousemove', { clientX: 140, clientY: 130 }))
    expect(store.panelPosition.x).toBe(startX + 40)
    expect(store.panelPosition.y).toBe(110)
    document.dispatchEvent(new MouseEvent('mouseup'))
    document.dispatchEvent(new MouseEvent('mousemove', { clientX: 200, clientY: 200 }))
    expect(store.panelPosition.x).toBe(startX + 40)
  })

  it('resizes the panel from the resize handle', async () => {
    const store = useAssistantStore()
    store.panelState = 'docked'
    const wrapper = await mountPanel()
    const startW = store.panelSize.width
    await wrapper.find('.assistant-resize-handle').trigger('mousedown', { clientX: 0, clientY: 0 })
    document.dispatchEvent(new MouseEvent('mousemove', { clientX: 60, clientY: 0 }))
    expect(store.panelSize.width).toBe(startW + 60)
    document.dispatchEvent(new MouseEvent('mouseup'))
  })

  it('cycles the UI navigation speed when the feature is enabled', async () => {
    featureEnabledMock.mockReturnValue(true)
    const wrapper = await mountPanelOpen()
    const speedBtn = wrapper.find('.assistant-titlebar button[aria-label^="Speed:"]')
    expect(speedBtn.exists()).toBe(true)
    expect(speedBtn.text()).toContain('normal')
    await speedBtn.trigger('click')
    const { setActionSpeed } = await import('@/composables/useUiCommandExecutor')
    expect(vi.mocked(setActionSpeed)).toHaveBeenLastCalledWith('lightning')
    expect(wrapper.text()).toContain('Navigates as fast as possible')
    await speedBtn.trigger('click')
    expect(vi.mocked(setActionSpeed)).toHaveBeenLastCalledWith('review')
    await speedBtn.trigger('click')
    expect(vi.mocked(setActionSpeed)).toHaveBeenLastCalledWith('normal')
    const { resumeUiCommands } = await import('@/composables/useUiCommandExecutor')
    expect(vi.mocked(resumeUiCommands)).toHaveBeenCalled()
  })

  it('shows the review-mode bar with a resume control when speed is review', async () => {
    featureEnabledMock.mockReturnValue(true)
    localStorage.setItem('assistant-action-speed', 'review')
    const wrapper = await mountPanelOpen()
    const store = useAssistantStore()
    expect(store.activeSession).not.toBeNull()
    expect(wrapper.text()).toContain('Stops after each navigation')
    const resumeBtn = wrapper.findAll('button').find(b => b.text() === 'Resume')
    expect(resumeBtn).toBeDefined()
    const { resumeUiCommands } = await import('@/composables/useUiCommandExecutor')
    await resumeBtn!.trigger('click')
    expect(vi.mocked(resumeUiCommands)).toHaveBeenCalled()
  })

  it('hides the speed button when the UI-driving feature is disabled', async () => {
    featureEnabledMock.mockReturnValue(false)
    const wrapper = await mountPanelOpen()
    expect(wrapper.find('.assistant-titlebar button[aria-label^="Speed:"]').exists()).toBe(false)
  })

  it('loads the saved session on mount when it still exists', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'sess-1'
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/assistant/sessions') {
        return Promise.resolve({ data: { items: [makeSession()] }, error: undefined })
      }
      return Promise.resolve({ data: { items: [makeMessage()] }, error: undefined })
    })
    await mountPanel()
    expect(apiGet).toHaveBeenCalledWith('/api/v1/assistant/sessions/{session_id}/messages', {
      params: { path: { session_id: 'sess-1' } },
    })
    expect(store.messages).toHaveLength(1)
  })

  it('creates a new session on mount when there is no saved session', async () => {
    const store = useAssistantStore()
    await mountPanel()
    expect(apiPost).toHaveBeenCalledWith('/api/v1/assistant/sessions', expect.objectContaining({ body: expect.anything() }))
    expect(store.activeSessionId).toBe('sess-1')
    // Panel stays closed by default — user must explicitly open it
    expect(store.panelState).toBe('closed')
  })

  it('does not replace a saved session id that no longer exists on the server', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'sess-gone'
    await mountPanel()
    expect(apiPost).not.toHaveBeenCalledWith('/api/v1/assistant/sessions', expect.anything())
    expect(store.activeSessionId).toBe('sess-gone')
  })

  it('clamps stale persisted position into the viewport on mount when floating', async () => {
    const store = useAssistantStore()
    // Simulate a persisted position from a wider viewport (off-screen right)
    store.panelState = 'floating'
    store.panelPosition = { x: 9999, y: 80 }
    store.panelSize = { width: 440, height: 600 }
    await mountPanel()
    // After mount, position must be clamped inside the viewport
    expect(store.panelPosition.x).toBeLessThanOrEqual(window.innerWidth - 340)
    expect(store.panelPosition.y).toBeLessThanOrEqual(window.innerHeight - 100)
    expect(store.panelPosition.x).toBeGreaterThanOrEqual(8)
    expect(store.panelPosition.y).toBeGreaterThanOrEqual(8)
  })

  it('clamps stale persisted size into the viewport on mount', async () => {
    const store = useAssistantStore()
    store.panelState = 'docked'
    // Simulate a persisted size from a larger viewport
    store.panelSize = { width: 9999, height: 9999 }
    await mountPanel()
    // updateSize clamps width to window.innerWidth - 16 and height to window.innerHeight - 40
    expect(store.panelSize.width).toBeLessThanOrEqual(window.innerWidth - 16)
    expect(store.panelSize.height).toBeLessThanOrEqual(window.innerHeight - 40)
    expect(store.panelSize.width).toBeGreaterThanOrEqual(100)
    expect(store.panelSize.height).toBeGreaterThanOrEqual(100)
  })
})
