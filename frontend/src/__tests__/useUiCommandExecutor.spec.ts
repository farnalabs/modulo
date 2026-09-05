import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'
import {
  executeCommandBatch,
  setActionSpeed,
  pauseUiCommands,
  resumeUiCommands,
  abortUiCommands,
  isPaused,
} from '../composables/useUiCommandExecutor'
import { spotlight } from '../composables/useSpotlight'
import router from '@/router'

vi.mock('@/router', () => ({
  default: { push: vi.fn(async () => undefined) },
}))

vi.mock('../composables/useSpotlight', () => ({
  spotlight: { highlight: vi.fn(), dismiss: vi.fn() },
}))

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

describe('useUiCommandExecutor command surface', () => {
  // BroadcastChannel that grants every lock request immediately so element
  // commands (click/fill/select) run to completion instead of waiting out the
  // 5s lock timeout on a real (response-less) jsdom channel.
  class AutoGrantChannel {
    name: string
    listeners: Array<(e: MessageEvent) => void>

    constructor(name: string) {
      this.name = name
      this.listeners = []
    }

    addEventListener(type: string, handler: EventListener): void {
      if (type === 'message') this.listeners.push(handler as (e: MessageEvent) => void)
    }

    removeEventListener(type: string, handler: EventListener): void {
      if (type === 'message') this.listeners = this.listeners.filter((l) => l !== handler)
    }

    postMessage(msg: unknown): void {
      const data = msg as { type?: string; msgId?: string }
      if (data.type === 'lock-request') {
        for (const h of [...this.listeners]) {
          h({ data: { type: 'lock-response', msgId: data.msgId, granted: true, holder: null } } as MessageEvent)
        }
      }
    }
  }

  // jsdom has no rAF; route it through fake timers so waitForDomStable and
  // the wait command's polling loop advance deterministically.
  function stubRequestAnimationFrame(): void {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16))
  }

  const routerPush = router.push as unknown as Mock
  const highlightMock = spotlight.highlight as unknown as Mock
  const dismissMock = spotlight.dismiss as unknown as Mock

  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('BroadcastChannel', AutoGrantChannel)
    stubRequestAnimationFrame()
    // Reset the executor's module-level state between tests: unpause, drop
    // locks/abort controllers, and clear mock call history. The static import
    // keeps ONE executor instance for the whole describe — never resetModules
    // here, which would sever the executor's router/spotlight bindings.
    abortUiCommands()
    setActionSpeed('lightning')
    routerPush.mockClear()
    highlightMock.mockClear()
    dismissMock.mockClear()
  })

  afterEach(() => {
    abortUiCommands()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  /** Drains any navigation history left by earlier tests. */
  async function drainNavHistory(): Promise<void> {
    for (let i = 0; i < 5; i++) {
      const p = executeCommandBatch([{ id: `drain-${i}`, name: 'go_back', args: {} }])
      await vi.advanceTimersByTimeAsync(1000)
      const results = await p
      if (!results[0]?.success) return
    }
  }

  /** Runs a batch with generous fake-timer advancement for DOM waits/delays. */
  async function runBatch(
    commands: Array<{ id: string; name: string; args: Record<string, unknown> }>,
  ): Promise<Array<{ id: string; name: string; success: boolean; error?: string; result?: Record<string, unknown> }>> {
    const p = executeCommandBatch(commands)
    await vi.advanceTimersByTimeAsync(2000)
    return p
  }

  it('navigate pushes the route, waits for the DOM, and reports the URL', async () => {
    const results = await runBatch([{ id: '1', name: 'navigate', args: { path: '/runs' } }])
    expect(routerPush).toHaveBeenCalledWith('/runs')
    expect(results[0].success).toBe(true)
    expect(results[0].result?.url).toBe(location.href)
  })

  it('navigate retries once after a failed router push', async () => {
    routerPush.mockRejectedValueOnce(new Error('first push failed'))
    const results = await runBatch([{ id: '1', name: 'navigate', args: { path: '/runs' } }])
    expect(routerPush).toHaveBeenCalledTimes(2)
    expect(results[0].success).toBe(true)
  })

  it('go_back without history fails and with history returns to the previous URL', async () => {
    await drainNavHistory()
    const noHistory = await runBatch([{ id: '1', name: 'go_back', args: {} }])
    expect(noHistory[0].success).toBe(false)
    expect(noHistory[0].error).toBe('No navigation history')

    const withHistory = await runBatch([
      { id: '2', name: 'navigate', args: { path: '/runs' } },
      { id: '3', name: 'go_back', args: {} },
    ])
    expect(withHistory[1].success).toBe(true)
    expect(withHistory[1].result?.url).toBe(location.href)
  })

  it('click reports a missing element and clicks a found one', async () => {
    const missing = await runBatch([{ id: '1', name: 'click', args: { selector: '#nope' } }])
    expect(missing[0].success).toBe(false)
    expect(missing[0].error).toBe('Element not found: #nope')

    const clicked: string[] = []
    const btn = document.createElement('button')
    btn.dataset.testid = 'btn'
    btn.addEventListener('click', () => clicked.push('btn'))
    document.body.appendChild(btn)

    const hit = await runBatch([{ id: '2', name: 'click', args: { selector: 'btn' } }])
    expect(hit[0].success).toBe(true)
    expect(clicked).toEqual(['btn'])
  })

  it('click opens a combobox and waits before finishing', async () => {
    let opened = false
    const trigger = document.createElement('button')
    trigger.setAttribute('role', 'combobox')
    trigger.dataset.testid = 'cb'
    trigger.addEventListener('click', () => {
      opened = true
    })
    document.body.appendChild(trigger)

    const results = await runBatch([{ id: '1', name: 'click', args: { selector: 'cb' } }])
    expect(opened).toBe(true)
    expect(results[0].success).toBe(true)
  })

  it('fill sets an input value and fires input + change events', async () => {
    const events: string[] = []
    const input = document.createElement('input')
    input.dataset.testid = 'name-input'
    input.addEventListener('input', () => events.push('input'))
    input.addEventListener('change', () => events.push('change'))
    document.body.appendChild(input)

    const results = await runBatch([
      { id: '1', name: 'fill', args: { selector: 'name-input', value: 'hello' } },
    ])
    expect(results[0].success).toBe(true)
    expect(input.value).toBe('hello')
    expect(events).toEqual(['input', 'change'])
  })

  it('fill types into a contenteditable element', async () => {
    const el = document.createElement('div')
    el.dataset.testid = 'editor'
    el.setAttribute('contenteditable', 'true')
    document.body.appendChild(el)

    const results = await runBatch([
      { id: '1', name: 'fill', args: { selector: 'editor', value: 'typed text' } },
    ])
    expect(results[0].success).toBe(true)
    expect(el.textContent).toBe('typed text')
  })

  it('fill toggles a switch by clicking it', async () => {
    let toggled = false
    const sw = document.createElement('button')
    sw.setAttribute('role', 'switch')
    sw.dataset.testid = 'the-switch'
    sw.addEventListener('click', () => {
      toggled = true
    })
    document.body.appendChild(sw)

    const results = await runBatch([
      { id: '1', name: 'fill', args: { selector: 'the-switch', value: 'on' } },
    ])
    expect(results[0].success).toBe(true)
    expect(toggled).toBe(true)
  })

  it('fill types into a combobox trigger’s own command input', async () => {
    const trigger = document.createElement('div')
    trigger.setAttribute('role', 'combobox')
    trigger.dataset.testid = 'cb-fill'
    const inner = document.createElement('input')
    trigger.appendChild(inner)
    document.body.appendChild(trigger)

    const results = await runBatch([
      { id: '1', name: 'fill', args: { selector: 'cb-fill', value: 'query' } },
    ])
    expect(results[0].success).toBe(true)
    expect(inner.value).toBe('query')
  })

  it('fill falls back to a global combobox command input when the trigger has none', async () => {
    const trigger = document.createElement('button')
    trigger.setAttribute('role', 'combobox')
    trigger.dataset.testid = 'cb-empty'
    document.body.appendChild(trigger)
    const other = document.createElement('div')
    other.setAttribute('role', 'combobox')
    const globalInput = document.createElement('input')
    other.appendChild(globalInput)
    document.body.appendChild(other)

    const results = await runBatch([
      { id: '1', name: 'fill', args: { selector: 'cb-empty', value: 'global' } },
    ])
    expect(results[0].success).toBe(true)
    expect(globalInput.value).toBe('global')
  })

  it('fill rejects unsupported elements', async () => {
    const div = document.createElement('div')
    div.dataset.testid = 'plain-div'
    document.body.appendChild(div)
    const results = await runBatch([
      { id: '1', name: 'fill', args: { selector: 'plain-div', value: 'x' } },
    ])
    expect(results[0].success).toBe(false)
    expect(results[0].error).toBe('Unsupported element: div')
  })

  it('select picks a native option by value and by visible text', async () => {
    const changes: string[] = []
    const select = document.createElement('select')
    select.dataset.testid = 'fruit'
    for (const [value, label] of [['a', 'Apple'], ['b', 'Banana']] as const) {
      const opt = document.createElement('option')
      opt.value = value
      opt.textContent = label
      select.appendChild(opt)
    }
    select.addEventListener('change', () => changes.push(select.value))
    document.body.appendChild(select)

    const byValue = await runBatch([
      { id: '1', name: 'select', args: { selector: 'fruit', value: 'a' } },
    ])
    expect(byValue[0].success).toBe(true)
    expect(changes).toEqual(['a'])

    const byText = await runBatch([
      { id: '2', name: 'select', args: { selector: 'fruit', value: 'Banana' } },
    ])
    expect(byText[0].success).toBe(true)
    expect(changes).toEqual(['a', 'b'])
  })

  it('select reports a missing native option and unsupported elements', async () => {
    const select = document.createElement('select')
    select.dataset.testid = 'fruit'
    const opt = document.createElement('option')
    opt.value = 'a'
    select.appendChild(opt)
    document.body.appendChild(select)

    const missing = await runBatch([
      { id: '1', name: 'select', args: { selector: 'fruit', value: 'zzz' } },
    ])
    expect(missing[0].success).toBe(false)
    expect(missing[0].error).toBe('Option not found: zzz')

    const div = document.createElement('div')
    div.dataset.testid = 'not-a-select'
    document.body.appendChild(div)
    const unsupported = await runBatch([
      { id: '2', name: 'select', args: { selector: 'not-a-select', value: 'a' } },
    ])
    expect(unsupported[0].success).toBe(false)
    expect(unsupported[0].error).toBe('Unsupported element for select: DIV')
  })

  it('extract returns sanitized text (scripts stripped)', async () => {
    const host = document.createElement('div')
    host.dataset.testid = 'card'
    host.innerHTML = '<span>visible text</span><script>alert(1)<\/script>'
    document.body.appendChild(host)

    const found = await runBatch([{ id: '1', name: 'extract', args: { selector: 'card' } }])
    expect(found[0].success).toBe(true)
    expect(found[0].result?.text).toBe('visible text')
    expect(found[0].result?.selector).toBe('card')

    const missing = await runBatch([{ id: '2', name: 'extract', args: { selector: 'nope' } }])
    expect(missing[0].success).toBe(false)
    expect(missing[0].error).toBe('Element not found: nope')
  })

  it('extract_all collects every match with indices', async () => {
    for (const label of ['one', 'two']) {
      const el = document.createElement('p')
      el.className = 'row'
      el.textContent = label
      document.body.appendChild(el)
    }
    const results = await runBatch([
      { id: '1', name: 'extract_all', args: { selector: 'p.row' } },
    ])
    expect(results[0].success).toBe(true)
    expect(results[0].result?.count).toBe(2)
    const items = results[0].result?.items as Array<{ index: number; text: string }>
    expect(items.map((i) => i.text)).toEqual(['one', 'two'])
    expect(items.map((i) => i.index)).toEqual([0, 1])
  })

  it('get_page_interactables inventories visible interactive elements', async () => {
    const withTestid = document.createElement('button')
    withTestid.dataset.testid = 'save-btn'
    withTestid.textContent = 'Save'
    Object.defineProperty(withTestid, 'offsetWidth', { value: 20 })
    document.body.appendChild(withTestid)

    const withId = document.createElement('a')
    withId.id = 'docs-link'
    withId.textContent = 'Docs'
    Object.defineProperty(withId, 'offsetWidth', { value: 20 })
    document.body.appendChild(withId)

    const textOnly = document.createElement('button')
    textOnly.textContent = 'Press me'
    Object.defineProperty(textOnly, 'offsetWidth', { value: 20 })
    document.body.appendChild(textOnly)

    const empty = document.createElement('button')
    Object.defineProperty(empty, 'offsetWidth', { value: 20 })
    document.body.appendChild(empty)

    const results = await runBatch([{ id: '1', name: 'get_page_interactables', args: {} }])
    expect(results[0].success).toBe(true)
    const items = results[0].result?.items as Array<Record<string, unknown>>
    expect(items).toHaveLength(3)
    expect(items[0].testid).toBe('save-btn')
    expect(items[0].selector).toBe('[data-testid="save-btn"]')
    expect(items[1].selector).toBe('#docs-link')
    expect(String(items[2].selector)).toContain('nth-of-type')
    expect(items[2].text).toBe('Press me')
  })

  it('wait finds a present selector immediately, honours ms, and no-ops with no args', async () => {
    const found = document.createElement('div')
    found.dataset.testid = 'late-element'
    document.body.appendChild(found)

    const foundResult = await runBatch([
      { id: '1', name: 'wait', args: { selector: 'late-element' } },
    ])
    expect(foundResult[0].success).toBe(true)
    expect(foundResult[0].result).toEqual({ found: true, selector: 'late-element' })

    const timed = await runBatch([{ id: '2', name: 'wait', args: { ms: 120 } }])
    expect(timed[0].success).toBe(true)

    const immediate = await runBatch([{ id: '3', name: 'wait', args: {} }])
    expect(immediate[0].success).toBe(true)
  })

  it('wait reports a timeout when the selector never appears', async () => {
    const results = await runBatch([
      { id: '1', name: 'wait', args: { selector: 'ghost-element', timeout: 60 } },
    ])
    expect(results[0].success).toBe(false)
    expect(results[0].error).toBe('Timeout waiting for: ghost-element')
  })

  it('a command exceeding the 30s per-command budget times out', async () => {
    const p = executeCommandBatch([
      { id: '1', name: 'wait', args: { selector: 'never-appears', timeout: 45000 } },
    ])
    await vi.advanceTimersByTimeAsync(31000)
    const results = await p
    expect(results[0].success).toBe(false)
    expect(results[0].error).toBe('command_timeout')
    // Let the orphaned wait loop observe the element and exit cleanly.
    const late = document.createElement('div')
    late.dataset.testid = 'never-appears'
    document.body.appendChild(late)
    await vi.advanceTimersByTimeAsync(20000)
  })

  it('press dispatches keydown/keyup on the active element', async () => {
    const keys: string[] = []
    document.body.addEventListener('keydown', (e) => keys.push(`down:${e.key}`))
    document.body.addEventListener('keyup', (e) => keys.push(`up:${e.key}`))

    const results = await runBatch([{ id: '1', name: 'press', args: { key: 'Enter' } }])
    expect(results[0].success).toBe(true)
    expect(keys).toEqual(['down:Enter', 'up:Enter'])
  })

  it('get_url reports the current location', async () => {
    const results = await runBatch([{ id: '1', name: 'get_url', args: {} }])
    expect(results[0].success).toBe(true)
    expect(results[0].result?.url).toBe(location.href)
  })

  it('spotlight highlights a target or dismisses when absent', async () => {
    await runBatch([
      { id: '1', name: 'spotlight', args: { target: 'save-btn', message: 'Click here' } },
    ])
    expect(highlightMock).toHaveBeenCalledWith('save-btn', 'Click here')

    await runBatch([{ id: '2', name: 'spotlight', args: {} }])
    expect(dismissMock).toHaveBeenCalled()
  })

  it('unknown commands fail with a descriptive error', async () => {
    const results = await runBatch([{ id: '1', name: 'teleport', args: {} }])
    expect(results[0].success).toBe(false)
    expect(results[0].error).toBe('Unknown command: teleport')
  })

  it('paused batches wait for resume before executing', async () => {
    pauseUiCommands()
    expect(isPaused()).toBe(true)

    const p = executeCommandBatch([{ id: '1', name: 'get_url', args: {} }])
    await vi.advanceTimersByTimeAsync(100)
    resumeUiCommands()
    await vi.advanceTimersByTimeAsync(100)
    const results = await p
    expect(results[0].success).toBe(true)
    expect(isPaused()).toBe(false)
  })

  it('abortUiCommands cancels in-flight batches with cancelled_by_user', async () => {
    pauseUiCommands()
    const p = executeCommandBatch([
      { id: '1', name: 'get_url', args: {} },
      { id: '2', name: 'get_url', args: {} },
    ])
    await vi.advanceTimersByTimeAsync(100)
    abortUiCommands()
    const results = await p
    expect(results).toHaveLength(2)
    for (const r of results) {
      expect(r.success).toBe(false)
      expect(r.error).toBe('cancelled_by_user')
    }
  })

  it('a hidden document tab pauses execution until it becomes visible again', async () => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })

    const p = executeCommandBatch([{ id: '1', name: 'get_url', args: {} }])
    await vi.advanceTimersByTimeAsync(500)
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(500)
    const results = await p
    delete (document as unknown as Record<string, unknown>).visibilityState

    expect(results[0].success).toBe(true)
  })

  it('review action speed re-pauses after each command', async () => {
    setActionSpeed('review')
    const results = await runBatch([{ id: '1', name: 'get_url', args: {} }])
    expect(results[0].success).toBe(true)
    expect(isPaused()).toBe(true)
  })

  it('the default action speed delays 600ms between commands', async () => {
    setActionSpeed('normal')
    const p = executeCommandBatch([{ id: '1', name: 'get_url', args: {} }])
    await vi.advanceTimersByTimeAsync(1000)
    const results = await p
    expect(results[0].success).toBe(true)
  })
})
