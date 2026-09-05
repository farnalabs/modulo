import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import RemyChat from '../components/remy/RemyChat.vue'
import { useRemyStore } from '../composables/useRemyStore'

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
    featureEnabled: vi.fn((name: string) => name === 'remy_ui_driving'),
  })),
}))

const { connectStreamMock, disconnectStreamMock } = vi.hoisted(() => ({
  connectStreamMock: vi.fn(() => Promise.resolve()),
  disconnectStreamMock: vi.fn(() => Promise.resolve()),
}))

vi.mock('@/composables/useRemyStream', () => ({
  useRemyStream: vi.fn(() => ({
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

function mountChat(remyOnly: boolean) {
  return mount(RemyChat, { props: { remyOnly } })
}

async function triggerDeleteFlow(wrapper: ReturnType<typeof mountChat>) {
  await wrapper.find('.remy-input').setValue('/delete')
  await wrapper.find('.remy-input').trigger('keydown', { key: 'Enter' })
  await wrapper.find('.remy-delete-confirm button').trigger('click')
  await flushPromises()
}

describe('RemyChat remyOnly prop', () => {
  it('hides the permission/NOGO card when remyOnly, even with a pending permission', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-1',
      tools: [{ name: 'click', args: { selector: '.delete-btn' } }],
    }
    const wrapper = mountChat(true)
    expect(wrapper.find('.remy-permission-card').exists()).toBe(false)
  })

  it('still renders the permission card when NOT remyOnly (panel regression)', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-1',
      tools: [{ name: 'click', args: { selector: '.delete-btn' } }],
    }
    const wrapper = mountChat(false)
    expect(wrapper.find('.remy-permission-card').exists()).toBe(true)
  })

  it('does NOT auto-create a new session after deleting the last session in remy-only mode', async () => {
    const store = useRemyStore()
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
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.sessions = []
    const createSpy = vi.spyOn(store, 'createSession').mockResolvedValue(null as never)

    const wrapper = mountChat(false)
    await triggerDeleteFlow(wrapper)

    expect(createSpy).toHaveBeenCalled()
  })

  it('does NOT render the UI-executing indicator when remyOnly', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.isExecutingUi = true
    const wrapper = mountChat(true)
    expect(wrapper.find('.remy-executing-indicator').exists()).toBe(false)
  })

  it('renders the UI-executing indicator when NOT remyOnly (panel regression)', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.isExecutingUi = true
    const wrapper = mountChat(false)
    expect(wrapper.find('.remy-executing-indicator').exists()).toBe(true)
  })
})

describe('RemyChat analytics chart card', () => {
  it('renders a chart card + deep link for a successful query_analytics tool result', async () => {
    const store = useRemyStore()
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
    expect(wrapper.find('[data-testid="remy-analytics-card"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="analytics-chart"]').exists()).toBe(true)
    const link = wrapper.find('.remy-analytics-link')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('/analytics?group_by=day&date_from=2026-07-30&date_to=2026-08-06')
  })

  it('falls back to the generic tool card when the analytics result is not chartable', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-2',
      tool_name: 'query_analytics',
      success: true,
      result: { group_by: 'day', buckets: 'not-an-array' },
    })
    const wrapper = mountChat(false)
    expect(wrapper.find('[data-testid="remy-analytics-card"]').exists()).toBe(false)
    expect(wrapper.find('.remy-tool-card').exists()).toBe(true)
  })
})

describe('RemyChat intro and messages', () => {
  it('shows the intro message when a session is active with no messages and not streaming', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.messages = []
    const wrapper = mountChat(false)
    expect(wrapper.find('.remy-msg.assistant').exists()).toBe(true)
  })

  it('hides the intro message once messages exist', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendSystemMessage('turn separator')
    const wrapper = mountChat(false)
    expect(wrapper.find('.remy-turn-separator').exists()).toBe(true)
    expect(wrapper.find('.remy-messages .remy-msg.assistant').exists()).toBe(false)
  })

  it('shows the streaming indicator while streaming', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.isStreaming = true
    const wrapper = mountChat(false)
    expect(wrapper.find('.remy-streaming-indicator').exists()).toBe(true)
  })

  it('renders assistant markdown: bold, inline code and headings', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('### Heading\n**bold** and `code` text')
    const wrapper = mountChat(false)
    const html = wrapper.find('.remy-markdown').html()
    expect(html).toContain('remy-h3')
    expect(html).toContain('<strong>bold</strong>')
    expect(html).toContain('remy-inline-code')
  })

  it('renders fenced code blocks with the language attribute', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('```js\nconst x = 1\n```')
    const wrapper = mountChat(false)
    const html = wrapper.find('.remy-markdown').html()
    expect(html).toContain('<pre data-lang="js">')
    expect(html).toContain('const x = 1')
  })

  it('escapes HTML in assistant markdown', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('<img src=x onerror=alert(1)>')
    const wrapper = mountChat(false)
    const html = wrapper.find('.remy-markdown').html()
    expect(html).not.toContain('<img src=x')
    expect(html).toContain('&lt;img')
  })

  it('renders bullet lists as ul/li', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('- first\n- second')
    const wrapper = mountChat(false)
    const html = wrapper.find('.remy-markdown').html()
    expect(html).toContain('remy-ul')
    expect(html).toContain('remy-li')
  })

  it('derives the user initial from the token subject', async () => {
    const { getAccessToken } = await import('@/lib/api/client')
    const payload = btoa(JSON.stringify({ sub: 'duncan@farnalabs.com' }))
    vi.mocked(getAccessToken).mockReturnValue(`hdr.${payload}.sig`)
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.messages = [{ id: 'm1', session_id: 'session-1', role: 'user', content: 'hi', tool_calls_json: null, tool_results_json: null, token_count: null, parent_id: null, created_at: '2026-01-01T00:00:00Z' }]
    const wrapper = mountChat(false)
    expect(wrapper.find('.avatar-user').text()).toBe('D')
    vi.mocked(getAccessToken).mockReturnValue('mock-token')
  })

  it('falls back to ? when the token is unusable', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.messages = [{ id: 'm1', session_id: 'session-1', role: 'user', content: 'hi', tool_calls_json: null, tool_results_json: null, token_count: null, parent_id: null, created_at: '2026-01-01T00:00:00Z' }]
    const wrapper = mountChat(false)
    expect(wrapper.find('.avatar-user').text()).toBe('?')
  })
})

describe('RemyChat sending', () => {
  it('sends a typed message, clears the input and connects the stream', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    const { useRemyStream } = await import('@/composables/useRemyStream')
    void useRemyStream
    await wrapper.find('.remy-input').setValue('Hello Remy')
    await wrapper.find('button[aria-label="Send message"]').trigger('click')
    expect(sendSpy).toHaveBeenCalledWith('Hello Remy')
    expect((wrapper.find('.remy-input').element as HTMLTextAreaElement).value).toBe('')
    expect(connectStreamMock).toHaveBeenCalledWith('session-1', { excludeUiTools: false })
  })

  it('sends on Enter but not on Shift+Enter', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    await wrapper.find('.remy-input').setValue('line one')
    await wrapper.find('.remy-input').trigger('keydown', { key: 'Enter' })
    expect(sendSpy).toHaveBeenCalledTimes(1)
    await wrapper.find('.remy-input').setValue('line two')
    await wrapper.find('.remy-input').trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(sendSpy).toHaveBeenCalledTimes(1)
  })

  it('disables send while streaming or when the input is empty', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    const wrapper = mountChat(false)
    const sendBtn = wrapper.find('button[aria-label="Send message"]')
    expect(sendBtn.attributes('disabled')).toBeDefined()
    await wrapper.find('.remy-input').setValue('hello')
    expect(sendBtn.attributes('disabled')).toBeUndefined()
    store.isStreaming = true
    await wrapper.vm.$nextTick()
    expect(sendBtn.attributes('disabled')).toBeDefined()
  })

  it('does not send while the UI is executing', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.isExecutingUi = true
    const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    await wrapper.find('.remy-input').setValue('hello')
    await wrapper.find('button[aria-label="Send message"]').trigger('click')
    expect(sendSpy).not.toHaveBeenCalled()
  })

  it('copies an assistant message to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('copy me')
    const wrapper = mountChat(false)
    await wrapper.find('.remy-copy-btn').trigger('click')
    expect(writeText).toHaveBeenCalledWith('copy me')
  })
})

describe('RemyChat slash commands', () => {
  async function typeSlash(wrapper: ReturnType<typeof mountChat>, text: string) {
    await wrapper.find('.remy-input').setValue(text)
    await wrapper.find('.remy-input').trigger('input')
  }

  it('opens the slash menu on "/" and filters by prefix', async () => {
    const wrapper = mountChat(false)
    expect(wrapper.find('.remy-slash-menu').exists()).toBe(false)
    await typeSlash(wrapper, '/')
    expect(wrapper.find('.remy-slash-menu').exists()).toBe(true)
    expect(wrapper.findAll('.remy-slash-item')).toHaveLength(6)
    await typeSlash(wrapper, '/re')
    expect(wrapper.findAll('.remy-slash-item')).toHaveLength(1)
    expect(wrapper.find('.remy-slash-command').text()).toBe('/rename')
  })

  it('runs /help and appends a system message listing commands', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    const appendSpy = vi.spyOn(store, 'appendSystemMessage')
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/')
    await wrapper.findAll('.remy-slash-item').find(b => b.text().includes('/help'))!.trigger('click')
    expect(appendSpy).toHaveBeenCalledTimes(1)
    expect(appendSpy.mock.calls[0][0]).toContain('/rename')
    expect((wrapper.find('.remy-input').element as HTMLTextAreaElement).value).toBe('')
  })

  it('runs /exit and closes the panel', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/exit')
    await wrapper.find('.remy-slash-item').trigger('click')
    expect(store.panelState).toBe('closed')
  })

  it('runs /clear and empties the input', async () => {
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/clear')
    await wrapper.find('.remy-slash-item').trigger('click')
    expect((wrapper.find('.remy-input').element as HTMLTextAreaElement).value).toBe('')
  })

  it('only ever executes /rename without an argument (triggerRename) — typed arguments cannot reach the command', async () => {
    // BUG NOTE (FAR-578 report): the slash menu only opens while the input has
    // no spaces (onInput), so a command with arguments (e.g. "/rename New
    // Name") closes the menu and Enter routes the text to handleSend as a
    // chat message instead of the slash action. executeSlashCommand always
    // sees the bare "/rename" and falls back to triggerRename.
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    const renameSpy = vi.spyOn(store, 'renameSession')
    const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    await wrapper.find('.remy-input').setValue('/rename New Name')
    await wrapper.find('.remy-input').trigger('input')
    expect(wrapper.find('.remy-slash-menu').exists()).toBe(false)
    await wrapper.find('.remy-input').trigger('keydown', { key: 'Enter' })
    expect(renameSpy).not.toHaveBeenCalled()
    expect(sendSpy).toHaveBeenCalledWith('/rename New Name')
  })

  it('runs /rename without a name and triggers the panel rename UI', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    const triggerSpy = vi.spyOn(store, 'triggerRename')
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/rename')
    await wrapper.find('.remy-slash-item').trigger('click')
    expect(triggerSpy).toHaveBeenCalled()
  })

  it('runs /new and creates a session', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    const createSpy = vi.spyOn(store, 'createSession').mockResolvedValue(null as never)
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/')
    await wrapper.findAll('.remy-slash-item').find(b => b.text().includes('/new'))!.trigger('click')
    expect(createSpy).toHaveBeenCalled()
  })

  it('runs /delete and shows the delete confirmation', async () => {
    const api = await import('@/lib/api/client')
    vi.mocked(api.api.DELETE).mockClear()
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/delete')
    await wrapper.find('.remy-slash-item').trigger('click')
    expect(wrapper.find('.remy-delete-confirm').exists()).toBe(true)
    await wrapper.findAll('.remy-delete-confirm button').find(b => b.text() === 'Cancel')!.trigger('click')
    expect(wrapper.find('.remy-delete-confirm').exists()).toBe(false)
    expect(api.api.DELETE).not.toHaveBeenCalled()
  })

  it('completes a partial command with Enter instead of executing it', async () => {
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/re')
    await wrapper.find('.remy-input').trigger('keydown', { key: 'Enter' })
    expect((wrapper.find('.remy-input').element as HTMLTextAreaElement).value).toBe('/rename ')
    expect(wrapper.find('.remy-slash-menu').exists()).toBe(false)
  })

  it('navigates the slash menu with arrow keys', async () => {
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/')
    const first = wrapper.findAll('.remy-slash-item')[0]
    expect(first.classes()).toContain('active')
    await wrapper.find('.remy-input').trigger('keydown', { key: 'ArrowDown' })
    expect(wrapper.findAll('.remy-slash-item')[1].classes()).toContain('active')
    await wrapper.find('.remy-input').trigger('keydown', { key: 'ArrowUp' })
    expect(wrapper.findAll('.remy-slash-item')[0].classes()).toContain('active')
  })

  it('closes the slash menu with Escape', async () => {
    const wrapper = mountChat(false)
    await typeSlash(wrapper, '/')
    expect(wrapper.find('.remy-slash-menu').exists()).toBe(true)
    await wrapper.find('.remy-input').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('.remy-slash-menu').exists()).toBe(false)
  })
})

describe('RemyChat tool cards', () => {
  it('renders a tool card and toggles its details', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-9', tool_name: 'navigate', success: true, result: { path: '/pipelines' } })
    const wrapper = mountChat(false)
    const card = wrapper.find('.remy-tool-card')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('navigate')
    expect(card.text()).toContain('Completed')
    expect(card.find('.remy-tool-details').exists()).toBe(false)
    await card.find('.remy-tool-header').trigger('click')
    expect(wrapper.find('.remy-tool-details').exists()).toBe(true)
    expect(wrapper.find('.remy-tool-details').text()).toContain('Result:')
    await wrapper.find('.remy-tool-header').trigger('click')
    expect(wrapper.find('.remy-tool-details').exists()).toBe(false)
  })

  it('renders failed tool results with the failed badge', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({ tool_call_id: 'tc-10', tool_name: 'click', success: false, error: 'selector not found' })
    const wrapper = mountChat(false)
    expect(wrapper.find('.tool-badge').text()).toBe('Failed')
  })
})

describe('RemyChat permission requests', () => {
  it('describes known tools in human-friendly wording', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-1',
      tools: [
        { name: 'navigate', args: { path: '/pipelines' } },
        { name: 'click', args: { selector: '[data-testid="delete-btn"]' } },
        { name: 'fill', args: { selector: '.search', value: 'remy' } },
        { name: 'wait', args: { ms: 500 } },
      ],
    }
    const wrapper = mountChat(false)
    const text = wrapper.find('.remy-permission-card').text()
    expect(text).toContain('Navigate to /pipelines')
    expect(text).toContain("Click 'delete btn'")
    expect(text).toContain("Type into .search: 'remy'")
    expect(text).toContain('Wait 500ms')
  })

  it('disables the action buttons while a nogo countdown runs', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-2',
      tools: [{ name: 'click', args: { selector: '.x' }, nogo: true }],
    }
    const wrapper = mountChat(false)
    const denyBtn = wrapper.findAll('.remy-permission-actions button').find(b => b.text().startsWith('Deny'))
    expect(denyBtn!.attributes('disabled')).toBeDefined()
    expect(denyBtn!.text()).toContain('(3s)')
    expect(wrapper.text()).toContain('Destructive Page')
  })

  it('keeps the action buttons enabled when no nogo tool is present', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-3',
      tools: [{ name: 'click', args: { selector: '.x' } }],
    }
    const wrapper = mountChat(false)
    const allowBtn = wrapper.findAll('.remy-permission-actions button').find(b => b.text().startsWith('Allow Once'))
    expect(allowBtn!.attributes('disabled')).toBeUndefined()
  })

  it('approves with "Allow Once" and clears the pending request', async () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.pendingPermission = {
      request_id: 'req-4',
      tools: [{ name: 'navigate', args: { path: '/' } }],
    }
    const approveSpy = vi.spyOn(store, 'approvePermission').mockResolvedValue(undefined as never)
    const wrapper = mountChat(false)
    await wrapper.findAll('.remy-permission-actions button').find(b => b.text().startsWith('Allow Once'))!.trigger('click')
    expect(approveSpy).toHaveBeenCalledWith('req-4', 'approve')
  })
})
