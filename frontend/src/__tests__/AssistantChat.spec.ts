import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import AssistantChat from '../components/assistant/AssistantChat.vue'
import { useAssistantStore } from '../composables/useAssistantStore'

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
  api: {
    POST: vi.fn(() => Promise.resolve({ error: null, data: {} })),
    GET: vi.fn(() => Promise.resolve({ error: null, data: { items: [] } })),
    PATCH: vi.fn(() => Promise.resolve({ error: null, data: {} })),
    DELETE: vi.fn(() => Promise.resolve({ error: null, data: {} })),
  },
}))

vi.mock('@/stores/planStore', () => ({
  usePlanStore: vi.fn(() => ({
    featureEnabled: vi.fn((name: string) => name === 'assistant_ui_driving'),
  })),
}))

const { connectStreamMock, disconnectStreamMock } = vi.hoisted(() => ({
  connectStreamMock: vi.fn(() => Promise.resolve()),
  disconnectStreamMock: vi.fn(() => Promise.resolve()),
}))

vi.mock('@/composables/useAssistantStream', () => ({
  useAssistantStream: vi.fn(() => ({
    connectStream: connectStreamMock,
    disconnectStream: disconnectStreamMock,
    connected: { value: false },
  })),
}))

vi.mock('@/composables/useUiCommandExecutor', () => ({
  pauseUiCommands: vi.fn(),
  resumeUiCommands: vi.fn(),
  abortUiCommands: vi.fn(),
  executeCommandBatch: vi.fn(),
  isPaused: vi.fn(() => false),
}))

vi.mock('vue-echarts', () => ({
  default: { name: 'VChart', props: ['option'], template: '<div class="vchart-stub" />' },
}))
vi.mock('echarts', () => ({ default: {} }))

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  vi.restoreAllMocks()
})

function mountChat(assistantOnly: boolean) {
  return mount(AssistantChat, { props: { assistantOnly } })
}

async function triggerDeleteFlow(wrapper: ReturnType<typeof mountChat>) {
  await wrapper.find('.assistant-input').setValue('/delete')
  await wrapper.find('.assistant-input').trigger('keydown', { key: 'Enter' })
  await wrapper.find('.assistant-delete-confirm button').trigger('click')
  await flushPromises()
}

describe('AssistantChat assistantOnly prop', () => {
  it('hides the permission/NOGO card when assistantOnly, even with a pending permission', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-1',
      tools: [{ name: 'click', args: { selector: '.delete-btn' } }],
    }
    const wrapper = mountChat(true)
    expect(wrapper.find('.assistant-permission-card').exists()).toBe(false)
  })

  it('still renders the permission card when NOT assistantOnly (panel regression)', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-1',
      tools: [{ name: 'click', args: { selector: '.delete-btn' } }],
    }
    const wrapper = mountChat(false)
    expect(wrapper.find('.assistant-permission-card').exists()).toBe(true)
  })

  it('does NOT auto-create a new session after deleting the last session in assistant-only mode', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.sessions = []
    const createSpy = vi.spyOn(store, 'createSession').mockResolvedValue(null as never)
    const loadSpy = vi.spyOn(store, 'loadSession').mockResolvedValue(undefined as never)

    const wrapper = mountChat(true)
    await triggerDeleteFlow(wrapper)

    expect(createSpy).not.toHaveBeenCalled()
    expect(loadSpy).not.toHaveBeenCalled()
  })

  it('auto-creates a new session after deleting the last session in panel mode (regression)', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.sessions = []
    const createSpy = vi.spyOn(store, 'createSession').mockResolvedValue(null as never)

    const wrapper = mountChat(false)
    await triggerDeleteFlow(wrapper)

    expect(createSpy).toHaveBeenCalled()
  })

  it('does NOT render the UI-executing indicator when assistantOnly', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.isExecutingUi = true
    const wrapper = mountChat(true)
    expect(wrapper.find('.assistant-executing-indicator').exists()).toBe(false)
  })

  it('renders the UI-executing indicator when NOT assistantOnly (panel regression)', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.isExecutingUi = true
    const wrapper = mountChat(false)
    expect(wrapper.find('.assistant-executing-indicator').exists()).toBe(true)
  })
})

describe('AssistantChat analytics chart card', () => {
  it('renders a chart card + deep link for a successful query_analytics tool result', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-1',
      tool_name: 'query_analytics',
      success: true,
      result: {
        group_by: 'day',
        dimension: null,
        date_from: '2026-07-30',
        date_to: '2026-08-06',
        deep_link: '/analytics?group_by=day&date_from=2026-07-30&date_to=2026-08-06',
        buckets: [
          { date: '2026-08-01', count: 3 },
          { date: '2026-08-02', count: 5 },
        ],
      },
    })
    const wrapper = mountChat(false)
    await flushPromises()
    expect(wrapper.find('[data-testid="assistant-analytics-card"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="analytics-chart"]').exists()).toBe(true)
    const link = wrapper.find('.assistant-analytics-link')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('/analytics?group_by=day&date_from=2026-07-30&date_to=2026-08-06')
  })

  it('falls back to the generic tool card when the analytics result is not chartable', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-2',
      tool_name: 'query_analytics',
      success: true,
      result: { group_by: 'day', buckets: 'not-an-array' },
    })
    const wrapper = mountChat(false)
    expect(wrapper.find('[data-testid="assistant-analytics-card"]').exists()).toBe(false)
    expect(wrapper.find('.assistant-tool-card').exists()).toBe(true)
  })
})

describe('AssistantChat intro and messages', () => {
  it('shows the intro message when a session is active with no messages and not streaming', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.messages = []
    const wrapper = mountChat(false)
    expect(wrapper.find('.assistant-msg.assistant').exists()).toBe(true)
  })

  it('hides the intro message once messages exist', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendSystemMessage('turn separator')
    const wrapper = mountChat(false)
    expect(wrapper.find('.assistant-turn-separator').exists()).toBe(true)
    expect(wrapper.find('.assistant-messages .assistant-msg.assistant').exists()).toBe(false)
  })

  it('shows the streaming indicator while streaming', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.isStreaming = true
    const wrapper = mountChat(false)
    expect(wrapper.find('.assistant-streaming-indicator').exists()).toBe(true)
  })

  it('renders assistant markdown: bold, inline code and headings', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('### Heading\n**bold** and `code` text')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).toContain('assistant-h3')
    expect(html).toContain('<strong>bold</strong>')
    expect(html).toContain('assistant-inline-code')
  })

  it('renders fenced code blocks with the language attribute', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('```js\nconst x = 1\n```')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).toContain('<pre data-lang="js">')
    expect(html).toContain('const x = 1')
  })

  it('escapes HTML in assistant markdown', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('<img src=x onerror=alert(1)>')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).not.toContain('<img src=x')
    expect(html).toContain('&lt;img')
  })

  it('renders bullet lists as ul/li', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('- first\n- second')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).toContain('assistant-ul')
    expect(html).toContain('assistant-li')
  })

  it('derives the user initial from the token subject', async () => {
    const { getAccessToken } = await import('@/lib/api/client')
    const payload = btoa(JSON.stringify({ sub: 'duncan@farnalabs.com' }))
    vi.mocked(getAccessToken).mockReturnValue(`hdr.${payload}.sig`)
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.messages = [{ id: 'm1', session_id: 'session-1', role: 'user', content: 'hi', tool_calls_json: null, tool_results_json: null, token_count: null, parent_id: null, created_at: '2026-01-01T00:00:00Z' }]
    const wrapper = mountChat(false)
    expect(wrapper.find('.avatar-user').text()).toBe('D')
    vi.mocked(getAccessToken).mockReturnValue('mock-token')
  })

  it('falls back to ? when the token is unusable', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.messages = [{ id: 'm1', session_id: 'session-1', role: 'user', content: 'hi', tool_calls_json: null, tool_results_json: null, token_count: null, parent_id: null, created_at: '2026-01-01T00:00:00Z' }]
    const wrapper = mountChat(false)
    expect(wrapper.find('.avatar-user').text()).toBe('?')
  })
})

describe('AssistantChat sending', () => {
  it('sends a typed message, clears the input and connects the stream', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    const { useAssistantStream } = await import('@/composables/useAssistantStream')
    void useAssistantStream
    await wrapper.find('.assistant-input').setValue('Hello Assistant')
    await wrapper.find('button[aria-label="Send message"]').trigger('click')
    expect(sendSpy).toHaveBeenCalledWith('Hello Assistant')
    expect((wrapper.find('.assistant-input').element as HTMLTextAreaElement).value).toBe('')
    expect(connectStreamMock).toHaveBeenCalledWith('session-1', { excludeUiTools: false })
  })

  it('sends on Enter but not on Shift+Enter', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-input').setValue('line one')
    await wrapper.find('.assistant-input').trigger('keydown', { key: 'Enter' })
    expect(sendSpy).toHaveBeenCalledTimes(1)
    await wrapper.find('.assistant-input').setValue('line two')
    await wrapper.find('.assistant-input').trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(sendSpy).toHaveBeenCalledTimes(1)
  })

  it('disables send while streaming or when the input is empty', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    const wrapper = mountChat(false)
    const sendBtn = wrapper.find('button[aria-label="Send message"]')
    expect(sendBtn.attributes('disabled')).toBeDefined()
    await wrapper.find('.assistant-input').setValue('hello')
    expect(sendBtn.attributes('disabled')).toBeUndefined()
    store.isStreaming = true
    await wrapper.vm.$nextTick()
    expect(sendBtn.attributes('disabled')).toBeDefined()
  })

  it('does not send while the UI is executing', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.isExecutingUi = true
    const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-input').setValue('hello')
    await wrapper.find('button[aria-label="Send message"]').trigger('click')
    expect(sendSpy).not.toHaveBeenCalled()
  })

  it('copies an assistant message to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('copy me')
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-copy-btn').trigger('click')
    expect(writeText).toHaveBeenCalledWith('copy me')
  })
})

describe('AssistantChat slash commands', () => {
  async function typeSlash(wrapper: ReturnType<typeof mountChat>, text: string) {
    await wrapper.find('.assistant-input').setValue(text)
    await wrapper.find('.assistant-input').trigger('input')
  }

  it('opens the slash menu on "/" and filters by prefix', async () => {
    const wrapper = mountChat(false)
    expect(wrapper.find('.assistant-slash-menu').exists()).toBe(false)
    await typeSlash(wrapper, '/')
    expect(wrapper.find('.assistant-slash-menu').exists()).toBe(true)
    expect(wrapper.findAll('.assistant-slash-item')).toHaveLength(6)
    await typeSlash(wrapper, '/re')
    expect(wrapper.findAll('.assistant-slash-item')).toHaveLength(1)
    expect(wrapper.find('.assistant-slash-command').text()).toBe('/rename')
  })

  it('runs /help and appends a system message listing commands', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    const appendSpy = vi.spyOn(store, 'appendSystemMessage')
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/')
    await wrapper.findAll('.assistant-slash-item').find(b => b.text().includes('/help'))!.trigger('click')
    expect(appendSpy).toHaveBeenCalledTimes(1)
    expect(appendSpy.mock.calls[0][0]).toContain('/rename')
    expect((wrapper.find('.assistant-input').element as HTMLTextAreaElement).value).toBe('')
  })

  it('runs /exit and closes the panel', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/exit')
    await wrapper.find('.assistant-slash-item').trigger('click')
    expect(store.panelState).toBe('closed')
  })

  it('runs /clear and empties the input', async () => {
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/clear')
    await wrapper.find('.assistant-slash-item').trigger('click')
    expect((wrapper.find('.assistant-input').element as HTMLTextAreaElement).value).toBe('')
  })

  it('executes /rename with typed arguments on Enter instead of sending them as a chat message', async () => {
    // The slash menu stays open while arguments are typed, and Enter routes
    // the full input to the matching command so "/rename New Name" renames
    // the session (FAR-606) rather than going to sendMessage.
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    const renameSpy = vi.spyOn(store, 'renameSession').mockResolvedValue(true as never)
    const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-input').setValue('/rename New Name')
    await wrapper.find('.assistant-input').trigger('input')
    expect(wrapper.find('.assistant-slash-menu').exists()).toBe(true)
    expect(wrapper.find('.assistant-slash-command').text()).toBe('/rename')
    await wrapper.find('.assistant-input').trigger('keydown', { key: 'Enter' })
    expect(renameSpy).toHaveBeenCalledWith('session-1', 'New Name')
    expect(sendSpy).not.toHaveBeenCalled()
    expect((wrapper.find('.assistant-input').element as HTMLTextAreaElement).value).toBe('')
  })

  it('runs /rename without a name and triggers the panel rename UI', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    const triggerSpy = vi.spyOn(store, 'triggerRename')
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/rename')
    await wrapper.find('.assistant-slash-item').trigger('click')
    expect(triggerSpy).toHaveBeenCalled()
  })

  it('runs /new and creates a session', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    const createSpy = vi.spyOn(store, 'createSession').mockResolvedValue(null as never)
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/')
    await wrapper.findAll('.assistant-slash-item').find(b => b.text().includes('/new'))!.trigger('click')
    expect(createSpy).toHaveBeenCalled()
  })

  it('runs /delete and shows the delete confirmation', async () => {
    const api = await import('@/lib/api/client')
    vi.mocked(api.api.DELETE).mockClear()
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/delete')
    await wrapper.find('.assistant-slash-item').trigger('click')
    expect(wrapper.find('.assistant-delete-confirm').exists()).toBe(true)
    await wrapper.findAll('.assistant-delete-confirm button').find(b => b.text() === 'Cancel')!.trigger('click')
    expect(wrapper.find('.assistant-delete-confirm').exists()).toBe(false)
    expect(api.api.DELETE).not.toHaveBeenCalled()
  })

  it('completes a partial command with Enter instead of executing it', async () => {
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/re')
    await wrapper.find('.assistant-input').trigger('keydown', { key: 'Enter' })
    expect((wrapper.find('.assistant-input').element as HTMLTextAreaElement).value).toBe('/rename ')
    expect(wrapper.find('.assistant-slash-menu').exists()).toBe(false)
  })

  it('navigates the slash menu with arrow keys', async () => {
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/')
    const first = wrapper.findAll('.assistant-slash-item')[0]
    expect(first.classes()).toContain('active')
    await wrapper.find('.assistant-input').trigger('keydown', { key: 'ArrowDown' })
    expect(wrapper.findAll('.assistant-slash-item')[1].classes()).toContain('active')
    await wrapper.find('.assistant-input').trigger('keydown', { key: 'ArrowUp' })
    expect(wrapper.findAll('.assistant-slash-item')[0].classes()).toContain('active')
  })

  it('closes the slash menu with Escape', async () => {
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/')
    expect(wrapper.find('.assistant-slash-menu').exists()).toBe(true)
    await wrapper.find('.assistant-input').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('.assistant-slash-menu').exists()).toBe(false)
  })
})

describe('AssistantChat tool cards', () => {
  it('renders a tool card and toggles its details', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-9', tool_name: 'navigate', success: true, result: { path: '/pipelines' } })
    const wrapper = mountChat(false)
    const card = wrapper.find('.assistant-tool-card')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('navigate')
    expect(card.text()).toContain('Completed')
    expect(card.find('.assistant-tool-details').exists()).toBe(false)
    await card.find('.assistant-tool-header').trigger('click')
    expect(wrapper.find('.assistant-tool-details').exists()).toBe(true)
    expect(wrapper.find('.assistant-tool-details').text()).toContain('Result:')
    await wrapper.find('.assistant-tool-header').trigger('click')
    expect(wrapper.find('.assistant-tool-details').exists()).toBe(false)
  })

  it('renders failed tool results with the failed badge', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-10', tool_name: 'click', success: false, error: 'selector not found' })
    const wrapper = mountChat(false)
    expect(wrapper.find('.tool-badge').text()).toBe('Failed')
  })
})

describe('AssistantChat permission requests', () => {
  it('describes known tools in human-friendly wording', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-1',
      tools: [
        { name: 'navigate', args: { path: '/pipelines' } },
        { name: 'click', args: { selector: '[data-testid="delete-btn"]' } },
        { name: 'fill', args: { selector: '.search', value: 'assistant' } },
        { name: 'wait', args: { ms: 500 } },
      ],
    }
    const wrapper = mountChat(false)
    const text = wrapper.find('.assistant-permission-card').text()
    expect(text).toContain('Navigate to /pipelines')
    expect(text).toContain("Click 'delete btn'")
    expect(text).toContain("Type into .search: 'assistant'")
    expect(text).toContain('Wait 500ms')
  })

  it('disables the action buttons while a nogo countdown runs', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-2',
      tools: [{ name: 'click', args: { selector: '.x' }, nogo: true }],
    }
    const wrapper = mountChat(false)
    const denyBtn = wrapper.findAll('.assistant-permission-actions button').find(b => b.text().startsWith('Deny'))
    expect(denyBtn!.attributes('disabled')).toBeDefined()
    expect(denyBtn!.text()).toContain('(3s)')
    expect(wrapper.text()).toContain('Destructive Page')
  })

  it('keeps the action buttons enabled when no nogo tool is present', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-3',
      tools: [{ name: 'click', args: { selector: '.x' } }],
    }
    const wrapper = mountChat(false)
    const allowBtn = wrapper.findAll('.assistant-permission-actions button').find(b => b.text().startsWith('Allow Once'))
    expect(allowBtn!.attributes('disabled')).toBeUndefined()
  })

  it('approves with "Allow Once" and clears the pending request', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-4',
      tools: [{ name: 'navigate', args: { path: '/' } }],
    }
    const approveSpy = vi.spyOn(store, 'approvePermission').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    await wrapper.findAll('.assistant-permission-actions button').find(b => b.text().startsWith('Allow Once'))!.trigger('click')
    expect(approveSpy).toHaveBeenCalledWith('req-4', 'approve')
  })
})

// ---- Branch coverage: describeArgs various tool types ----
describe('AssistantChat — describeArgs branches', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('describes extract, extract_all, get_page_interactables, get_url, press and go_back tools', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-1',
      tools: [
        { name: 'extract', args: { selector: '[data-testid="name"]' } },
        { name: 'extract_all', args: { selector: '.items' } },
        { name: 'get_page_interactables', args: {} },
        { name: 'get_url', args: {} },
        { name: 'press', args: { key: 'Escape' } },
        { name: 'go_back', args: {} },
      ],
    }
    const wrapper = mountChat(false)
    const text = wrapper.find('.assistant-permission-card').text()
    expect(text).toContain('Read text from name')
    expect(text).toContain("Read text from all '.items' elements")
    expect(text).toContain('Discover all clickable')
    expect(text).toContain('Get current page URL')
    expect(text).toContain("Press 'Escape' key")
    expect(text).toContain('Go back to previous page')
  })

  it('describes wait with selector and unknown tool as empty string', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-2',
      tools: [
        { name: 'wait', args: { selector: '.spinner' } },
        { name: 'custom_tool', args: {} },
      ],
    }
    const wrapper = mountChat(false)
    const text = wrapper.find('.assistant-permission-card').text()
    expect(text).toContain("Wait for '.spinner' to appear")
  })
})

// ---- Branch coverage: renderMarkdown various paths ----
describe('AssistantChat — renderMarkdown branches', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('renders heading levels 1 and 2', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('# H1\n## H2')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).toContain('assistant-h1')
    expect(html).toContain('assistant-h2')
  })

  it('renders italic text', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('*italic*')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).toContain('<em>italic</em>')
  })

  it('renders paragraphs from double newlines', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('para one\n\npara two')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).toContain('assistant-p')
  })

  it('wraps plain text in a paragraph tag', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('just text')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).toContain('<p class="assistant-p">')
  })

  it('renders fenced code block without language', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('```\nno lang\n```')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).toContain('<pre><code')
    expect(html).toContain('no lang')
  })

  it('handles empty text gracefully', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('')
    const wrapper = mountChat(false)
    const html = wrapper.find('.assistant-markdown').html()
    expect(html).toBeTruthy()
  })
})

// ---- Branch coverage: toggleToolExpand ----
describe('AssistantChat — toggleToolExpand', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('expands and collapses a tool card', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-1', tool_name: 'navigate', success: true, result: { path: '/runs' } })
    const wrapper = mountChat(false)

    // Initially collapsed
    expect(wrapper.find('.assistant-tool-details').exists()).toBe(false)

    // Expand
    await wrapper.find('.assistant-tool-header').trigger('click')
    expect(wrapper.find('.assistant-tool-details').exists()).toBe(true)

    // Collapse
    await wrapper.find('.assistant-tool-header').trigger('click')
    expect(wrapper.find('.assistant-tool-details').exists()).toBe(false)
  })
})

// ---- Branch coverage: formatToolDetails various paths ----
describe('AssistantChat — formatToolDetails branches', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('formats tool details with result and error', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-1', tool_name: 'click', success: false, result: { text: 'found' }, error: 'selector not found' })
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-tool-header').trigger('click')
    const details = wrapper.find('.assistant-tool-details').text()
    expect(details).toContain('Result:')
    expect(details).toContain('Error:')
    expect(details).toContain('selector not found')
  })

  it('formats tool details with string result', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-2', tool_name: 'extract', success: true, result: 'some text' })
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-tool-header').trigger('click')
    const details = wrapper.find('.assistant-tool-details').text()
    expect(details).toContain('some text')
  })

  it('formats tool details without result or error', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-3', tool_name: 'navigate', success: true })
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-tool-header').trigger('click')
    const details = wrapper.find('.assistant-tool-details').text()
    expect(details).toContain('Tool: navigate')
    expect(details).not.toContain('Result:')
    expect(details).not.toContain('Error:')
  })
})

// ---- Branch coverage: isAnalyticsChartMessage various false paths ----
describe('AssistantChat — isAnalyticsChartMessage false paths', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('does not render analytics card for a non-tool_result message', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('not a tool result')
    const wrapper = mountChat(false)
    expect(wrapper.find('[data-testid="assistant-analytics-card"]').exists()).toBe(false)
  })

  it('does not render analytics card for a failed tool result', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-fail', tool_name: 'query_analytics', success: false, result: {} })
    const wrapper = mountChat(false)
    expect(wrapper.find('[data-testid="assistant-analytics-card"]').exists()).toBe(false)
  })

  it('does not render analytics card for a non-query_analytics tool', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-nav', tool_name: 'navigate', success: true, result: {} })
    const wrapper = mountChat(false)
    expect(wrapper.find('[data-testid="assistant-analytics-card"]').exists()).toBe(false)
  })
})

// ---- Branch coverage: analyticsDeepLinkFor undefined path ----
describe('AssistantChat — analyticsDeepLinkFor undefined', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('hides deep link when result has no deep_link', () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-nolink',
      tool_name: 'query_analytics',
      success: true,
      result: { group_by: 'day', buckets: [{ date: '2026-01-01', count: 1 }] },
    })
    const wrapper = mountChat(false)
    expect(wrapper.find('.assistant-analytics-link').exists()).toBe(false)
  })
})

// ---- Branch coverage: copyMessage catch path ----
describe('AssistantChat — copyMessage catch', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('does not throw when clipboard.writeText rejects', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('not allowed'))
    Object.assign(navigator, { clipboard: { writeText } })
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.appendToken('copy me')
    const wrapper = mountChat(false)
    // Should not throw
    await wrapper.find('.assistant-copy-btn').trigger('click')
    expect(writeText).toHaveBeenCalled()
  })
})

// ---- Branch coverage: slash menu empty list and Tab key ----
describe('AssistantChat — slash menu edge cases', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('shows empty message when no commands match the filter', async () => {
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-input').setValue('/zzzzz')
    await wrapper.find('.assistant-input').trigger('input')
    expect(wrapper.find('.assistant-slash-menu').exists()).toBe(true)
    expect(wrapper.find('.assistant-slash-empty').exists()).toBe(true)
  })

  it('completes a partial command with Tab key', async () => {
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-input').setValue('/re')
    await wrapper.find('.assistant-input').trigger('input')
    await wrapper.find('.assistant-input').trigger('keydown', { key: 'Tab' })
    expect((wrapper.find('.assistant-input').element as HTMLTextAreaElement).value).toBe('/rename ')
  })
})

// ---- Branch coverage: deleteCurrentSession with remaining sessions ----
describe('AssistantChat — deleteCurrentSession loads first session', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('loads the first remaining session after deleting the current one', async () => {
    const store = useAssistantStore()
    store.activeSessionId = 'session-1'
    store.sessions = [{ id: 'session-2', name: 'Session 2' } as any]
    const loadSpy = vi.spyOn(store, 'loadSession').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    await wrapper.find('.assistant-input').setValue('/delete')
    await wrapper.find('.assistant-input').trigger('keydown', { key: 'Enter' })
    await wrapper.find('.assistant-delete-confirm button').trigger('click')
    await flushPromises()
    expect(loadSpy).toHaveBeenCalledWith('session-2')
  })
})
