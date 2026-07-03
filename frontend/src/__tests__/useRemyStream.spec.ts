import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useRemyStore } from '../composables/useRemyStore'
import { useRemyStream } from '../composables/useRemyStream'

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
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
    const store = setupStore()
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

    const abortSpy = vi.fn()
    global.fetch = vi.fn().mockReturnValue({
      ok: true,
      body: new ReadableStream({
        start(controller) {
          // Never close — simulate long stream
          controller.enqueue(new TextEncoder().encode('event: ping\ndata: {}\n\n'))
        },
      }),
      signal: { aborted: false },
    })

    await connectStream('session-1')
    disconnectStream()

    const store = useRemyStore()
    expect(store.isStreaming).toBe(false)
  })
})
