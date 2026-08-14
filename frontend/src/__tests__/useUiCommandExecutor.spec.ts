import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class FakeBroadcastChannel {
  static instance: FakeBroadcastChannel | null = null
  name: string
  listeners: Array<{ type: string; handler: EventListener }>
  posted: unknown[]
  addEventListener: ReturnType<typeof vi.fn>
  removeEventListener: ReturnType<typeof vi.fn>
  postMessage: ReturnType<typeof vi.fn>

  constructor(name: string) {
    this.name = name
    this.listeners = []
    this.posted = []
    this.addEventListener = vi.fn((type: string, handler: EventListener) => {
      this.listeners.push({ type, handler })
    })
    this.removeEventListener = vi.fn((type: string, handler: EventListener) => {
      this.listeners = this.listeners.filter(l => !(l.type === type && l.handler === handler))
    })
    this.postMessage = vi.fn((msg: unknown) => {
      this.posted.push(msg)
    })
    FakeBroadcastChannel.instance = this
  }
}

describe('useUiCommandExecutor lock listener cleanup', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel)
    vi.resetModules()
  })

  afterEach(() => {
    FakeBroadcastChannel.instance = null
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.resetModules()
    document.body.innerHTML = ''
  })

  it('removes the message listener when a lock acquisition times out', async () => {
    const { executeCommandBatch } = await import('../composables/useUiCommandExecutor')

    // No lock-response ever arrives: the acquire must time out (5s default).
    const batchPromise = executeCommandBatch([{ id: '1', name: 'click', args: { selector: '#btn' } }])
    await vi.advanceTimersByTimeAsync(6000)
    const results = await batchPromise

    expect(results[0].success).toBe(false)
    expect(results[0].error).toContain('Could not acquire lock')

    const channel = FakeBroadcastChannel.instance
    expect(channel).not.toBeNull()
    // The fix: on lock-acquisition timeout the per-request listener must be
    // removed. Before the fix only `resolved` was set and the listener leaked
    // on the shared channel forever (the unique msgId would never match again).
    expect(channel!.removeEventListener).toHaveBeenCalledWith('message', expect.any(Function))
    // Only the module-level lock-request handler may remain on the channel.
    expect(channel!.listeners.filter(l => l.type === 'message')).toHaveLength(1)
  })
})
