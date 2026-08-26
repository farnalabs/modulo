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

describe('useUiCommandExecutor select → combobox / scoped option lookup', () => {
  // A BroadcastChannel that grants every lock-request immediately (simulating a
  // second tab responding) so the select command can run to completion. The
  // existing describe above tests the lock-timeout path with a non-responding
  // channel; this one exercises the success path.
  class GrantingBroadcastChannel {
    name: string
    listeners: Array<(e: MessageEvent) => void>

    constructor(name: string) {
      this.name = name
      this.listeners = []
    }

    addEventListener(type: string, handler: EventListener) {
      if (type === 'message') this.listeners.push(handler as (e: MessageEvent) => void)
    }

    removeEventListener(type: string, handler: EventListener) {
      if (type === 'message') this.listeners = this.listeners.filter(l => l !== handler)
    }

    postMessage(msg: unknown) {
      const data = msg as { type?: string; msgId?: string }
      if (data.type === 'lock-request') {
        for (const h of this.listeners) {
          h({ data: { type: 'lock-response', msgId: data.msgId, granted: true, holder: null } } as MessageEvent)
        }
      }
    }
  }

  beforeEach(() => {
    vi.stubGlobal('BroadcastChannel', GrantingBroadcastChannel)
    vi.resetModules()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.resetModules()
    document.body.innerHTML = ''
  })

  it('select resolves a teleported combobox option after opening the popover', async () => {
    const { executeCommandBatch, setActionSpeed } = await import('../composables/useUiCommandExecutor')
    setActionSpeed('lightning')

    const trigger = document.createElement('button')
    trigger.setAttribute('role', 'combobox')
    trigger.dataset.testid = 'cb-trigger'
    trigger.textContent = 'Pick one'
    document.body.appendChild(trigger)

    // The option does not exist until the trigger is clicked — simulating a
    // teleported overlay that only renders at body level once the popover
    // opens. Without the click-to-open fix the option is never in the DOM, so
    // this fails (the pre-fix code queried el.querySelector on the trigger).
    const option = document.createElement('span')
    option.dataset.value = 'alpha'
    option.textContent = 'Alpha'
    option.addEventListener('click', () => {
      option.dataset.clicked = 'true'
    })
    trigger.addEventListener('click', () => {
      const listbox = document.createElement('div')
      listbox.setAttribute('role', 'listbox')
      listbox.appendChild(option)
      document.body.appendChild(listbox)
    })

    const results = await executeCommandBatch([
      { id: '1', name: 'select', args: { selector: '[data-testid="cb-trigger"]', value: 'alpha' } },
    ])

    expect(results[0].success).toBe(true)
    expect(option.dataset.clicked).toBe('true')
  })

  it('select prefers the trigger/overlay option over a stray page-level data-value', async () => {
    const { executeCommandBatch, setActionSpeed } = await import('../composables/useUiCommandExecutor')
    setActionSpeed('lightning')

    // A stray [data-value] elsewhere on the page, unrelated to the trigger and
    // NOT inside any listbox/menu overlay. A document-scoped query would match
    // this element first (it appears earlier in the document).
    const stray = document.createElement('span')
    stray.dataset.value = 'alpha'
    stray.textContent = 'stray'
    stray.addEventListener('click', () => {
      stray.dataset.clicked = 'true'
    })
    document.body.appendChild(stray)

    const trigger = document.createElement('button')
    trigger.setAttribute('role', 'combobox')
    trigger.dataset.testid = 'cb-trigger'
    trigger.textContent = 'Pick one'
    document.body.appendChild(trigger)

    // The real option only exists once the popover opens (teleported overlay
    // rendered as role="listbox" at body level, appended after the stray).
    const option = document.createElement('span')
    option.dataset.value = 'alpha'
    option.textContent = 'Alpha'
    option.addEventListener('click', () => {
      option.dataset.clicked = 'true'
    })
    trigger.addEventListener('click', () => {
      const listbox = document.createElement('div')
      listbox.setAttribute('role', 'listbox')
      listbox.appendChild(option)
      document.body.appendChild(listbox)
    })

    const results = await executeCommandBatch([
      { id: '1', name: 'select', args: { selector: '[data-testid="cb-trigger"]', value: 'alpha' } },
    ])

    expect(results[0].success).toBe(true)
    // The scoped query must click the listbox option, not the stray element.
    expect(option.dataset.clicked).toBe('true')
    expect(stray.dataset.clicked).toBeUndefined()
  })
})
