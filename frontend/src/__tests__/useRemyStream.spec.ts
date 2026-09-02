import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useRemyStore } from '../composables/useRemyStore'
import { useRemyStream } from '../composables/useRemyStream'
import { executeCommandBatch } from '../composables/useUiCommandExecutor'

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
}))

vi.mock('../stores/planStore', () => ({
  usePlanStore: vi.fn(() => ({
    featureEnabled: vi.fn((name: string) => {
      if (name === 'remy_ui_driving') return true
      return false
    }),
  })),
}))

vi.mock('../composables/useUiCommandExecutor', () => ({
  executeCommandBatch: vi.fn(() => Promise.resolve([{ name: 'get_url', success: true }])),
  isPaused: vi.fn(() => false),
  resumeUiCommands: vi.fn(),
}))

function createMockSSEStream(events: Array<{ event: string; data: unknown }>): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  const chunks = events
    .map(e => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`)
    .join('')
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(chunks))
      controller.close()
    },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('useRemyStream', () => {
  function setupStore() {
    const store = useRemyStore()
    store.sessions = [
      {
        id: 'session-1',
        user_id: 'user-1',
        name: 'Test Session',
        provider: 'anthropic',
        model: 'claude-sonnet-4-20250514',
        context_window_tokens: 200000,
        message_count: 1,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      } as any,
    ]
    store.activeSessionId = 'session-1'
    store.messages = [
      {
        id: 'msg-1',
        session_id: 'session-1',
        role: 'user',
        content: 'Hello',
        tool_calls_json: null,
        tool_results_json: null,
        token_count: null,
        parent_id: null,
        created_at: new Date().toISOString(),
      },
    ]
    return store
  }

  it('permission_request event sets pendingPermission in store', async () => {
    const store = setupStore()
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: createMockSSEStream([
        { event: 'permission_request', data: { request_id: 'req-1', tools: [{ name: 'click', args: { selector: '.delete-btn' } }] } },
      ]),
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    expect(store.pendingPermission).toEqual({
      request_id: 'req-1',
      tools: [{ name: 'click', args: { selector: '.delete-btn' } }],
    })
  })

  it('ui_command_batch event executes commands and POSTs results', async () => {
    setupStore()
    let postedBody: any = null
    global.fetch = vi.fn().mockImplementation((url: string, opts?: any) => {
      if (url === '/api/v1/remy/sessions/session-1/stream') {
        return Promise.resolve({
          ok: true,
          body: createMockSSEStream([
            { event: 'ui_command_batch', data: { commands: [{ id: 'cmd-1', name: 'get_url', args: {} }] } },
          ]),
        })
      }
      if (url.includes('/ui-command-results')) {
        postedBody = JSON.parse(opts?.body || '{}')
        return Promise.resolve({ ok: true })
      }
      return Promise.reject(new Error(`Unexpected fetch: ${url}`))
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    expect(postedBody).not.toBeNull()
    expect(postedBody.results).toBeDefined()
    expect(postedBody.results[0].name).toBe('get_url')
    expect(postedBody.results[0].success).toBe(true)
  })

  it('turn_separator event appends summary message', async () => {
    const store = setupStore()
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: createMockSSEStream([
        { event: 'turn_separator', data: { label: '--- turn boundary ---' } },
      ]),
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    const lastMsg = store.messages[store.messages.length - 1]
    expect(lastMsg.role).toBe('summary')
    expect(lastMsg.content).toBe('--- turn boundary ---')
  })

  it('abort_summary event appends system message and stops streaming', async () => {
    const store = setupStore()
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: createMockSSEStream([
        { event: 'abort_summary', data: { summary: '2 completed, 1 skipped', completed: 2, skipped: 1 } },
      ]),
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    const lastMsg = store.messages[store.messages.length - 1]
    expect(lastMsg.role).toBe('summary')
    expect(lastMsg.content).toBe('2 completed, 1 skipped')
    expect(store.isStreaming).toBe(false)
  })

  it('error event sets store error', async () => {
    const store = setupStore()
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: createMockSSEStream([
        { event: 'error', data: { detail: 'Something went wrong' } },
      ]),
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    expect(store.error).toBe('Something went wrong')
  })

  it('done event sets isStreaming to false', async () => {
    const store = setupStore()
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: createMockSSEStream([
        { event: 'done', data: { message_id: 'msg-42' } },
      ]),
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    expect(store.isStreaming).toBe(false)
  })

  it('token event appends to store', async () => {
    const store = setupStore()
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: createMockSSEStream([
        { event: 'token', data: { token: 'Hello' } },
        { event: 'token', data: { token: ' world' } },
      ]),
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    const lastMsg = store.messages[store.messages.length - 1]
    expect(lastMsg.role).toBe('assistant')
    expect(lastMsg.content).toBe('Hello world')
  })

  it('handles fetch failure gracefully', async () => {
    const store = setupStore()
    global.fetch = vi.fn().mockRejectedValue(new Error('Network error'))

    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    expect(store.error).toBe('Network error')
  })

  it('handles non-ok response', async () => {
    const store = setupStore()
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      statusText: 'Forbidden',
      body: null,
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    expect(store.error).toBe('Access denied. Contact your admin.')
  })

  it('disconnectStream aborts and resets state', async () => {
    setupStore()
    const { connectStream, disconnectStream } = useRemyStream()

    global.fetch = vi.fn().mockReturnValue({
      ok: true,
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('event: ping\ndata: {}\n\n'))
          controller.close()
        },
      }),
    })

    await connectStream('session-1')
    disconnectStream()

    const store = useRemyStore()
    expect(store.isStreaming).toBe(false)
  })

  it('POST body omits exclude_ui_tools by default and drops page_context when opted in', async () => {
    const store = setupStore()
    store.pageContext = { route: '/dashboard', params: { id: '1' }, entities: ['pipeline'] }
    const bodies: any[] = []
    global.fetch = vi.fn().mockImplementation((url: string, opts?: any) => {
      if (url.includes('/stream')) {
        bodies.push(JSON.parse(opts?.body || '{}'))
        return Promise.resolve({ ok: true, body: createMockSSEStream([{ event: 'done', data: { message_id: 'm1' } }]) })
      }
      return Promise.reject(new Error(`Unexpected fetch: ${url}`))
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')
    expect(bodies[0].exclude_ui_tools).toBeUndefined()
    expect(bodies[0].page_context).toContain('Page: /dashboard')

    await connectStream('session-1', { excludeUiTools: true })
    expect(bodies[1].exclude_ui_tools).toBe(true)
    expect(bodies[1].page_context).toBeUndefined()
  })

  it('two sequential sends both complete', async () => {
    setupStore()
    const streamCalls: number[] = []
    global.fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/stream')) {
        streamCalls.push(1)
        return Promise.resolve({ ok: true, body: createMockSSEStream([{ event: 'done', data: { message_id: 'm' } }]) })
      }
      return Promise.reject(new Error(`Unexpected fetch: ${url}`))
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1')
    await connectStream('session-1')

    const store = useRemyStore()
    expect(streamCalls.length).toBe(2)
    expect(store.isStreaming).toBe(false)
  })

  it('abort-then-send is serialized — replacement stream runs cleanly (identity guard)', async () => {
    const store = setupStore()
    let releaseFirst!: (value: unknown) => void
    const firstFetch = new Promise<unknown>((resolve) => { releaseFirst = resolve })
    let secondStreamCalls = 0
    global.fetch = vi
      .fn()
      .mockReturnValueOnce(firstFetch)
      .mockImplementationOnce(() => {
        secondStreamCalls++
        return Promise.resolve({
          ok: true,
          body: createMockSSEStream([
            { event: 'token', data: { token: 'reply' } },
            { event: 'done', data: { message_id: 'm2' } },
          ]),
        })
      })

    const { connectStream, disconnectStream } = useRemyStream()
    const first = connectStream('session-1')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(store.isStreaming).toBe(true)

    const teardown = disconnectStream()
    releaseFirst({ ok: true, body: createMockSSEStream([{ event: 'done', data: { message_id: 'm1' } }]) })
    await Promise.all([first, teardown])
    expect(store.isStreaming).toBe(false)

    await connectStream('session-1')
    expect(secondStreamCalls).toBe(1)
    expect(store.isStreaming).toBe(false)
    expect(store.messages[store.messages.length - 1].content).toBe('reply')
  })

  it('refuses a second same-session stream while one is in flight', async () => {
    setupStore()
    let releaseFirst!: (value: unknown) => void
    const firstFetch = new Promise<unknown>((resolve) => { releaseFirst = resolve })
    global.fetch = vi.fn().mockReturnValueOnce(firstFetch)

    const { connectStream } = useRemyStream()
    const first = connectStream('session-1')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(useRemyStore().isStreaming).toBe(true)

    await connectStream('session-1')

    expect(global.fetch).toHaveBeenCalledTimes(1)
    releaseFirst({ ok: true, body: createMockSSEStream([{ event: 'done', data: { message_id: 'm1' } }]) })
    await first
    expect(useRemyStore().isStreaming).toBe(false)
  })

  it('starts a stream after sendMessage pre-sets isStreaming (production send flow)', async () => {
    const store = setupStore()
    let streamCalled = 0
    global.fetch = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/remy/sessions/session-1/stream') {
        streamCalled++
        return Promise.resolve({
          ok: true,
          body: createMockSSEStream([
            { event: 'token', data: { token: 'reply' } },
            { event: 'done', data: { message_id: 'm1' } },
          ]),
        })
      }
      return Promise.reject(new Error(`Unexpected fetch: ${url}`))
    })

    await store.sendMessage('Hello')
    const { connectStream } = useRemyStream()
    await connectStream('session-1')

    expect(streamCalled).toBe(1)
    expect(store.messages[store.messages.length - 1].content).toBe('reply')
    expect(store.isStreaming).toBe(false)
  })

  it('remy-only mode does not execute ui_command_batch (defense-in-depth)', async () => {
    setupStore()
    const postedBatches: any[] = []
    global.fetch = vi.fn().mockImplementation((url: string, opts?: any) => {
      if (url === '/api/v1/remy/sessions/session-1/stream') {
        return Promise.resolve({
          ok: true,
          body: createMockSSEStream([{ event: 'ui_command_batch', data: { commands: [{ id: 'cmd-1', name: 'click', args: { selector: '.x' } }] } }]),
        })
      }
      if (url.includes('/ui-command-results')) {
        postedBatches.push(JSON.parse(opts?.body || '{}'))
        return Promise.resolve({ ok: true })
      }
      return Promise.reject(new Error(`Unexpected fetch: ${url}`))
    })

    const { connectStream } = useRemyStream()
    await connectStream('session-1', { excludeUiTools: true })

    expect(executeCommandBatch).not.toHaveBeenCalled()
    expect(postedBatches.length).toBe(1)
    expect(postedBatches[0].results).toEqual([])
  })
})
