import { formatApiError } from '../lib/api/formatError'
import { spotlight } from './useSpotlight'

import router from '@/router'

const TAB_ID = typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36)

interface LockState {
  selector: string
  tabId: string
  acquiredAt: number
}

const heldLocks = new Map<string, LockState>()
let _lockChannel: BroadcastChannel | null = null
let _lockChannelInitialized = false
let _beforeUnloadRegistered = false

function getLockChannel(): BroadcastChannel | null {
  if (_lockChannelInitialized) return _lockChannel
  _lockChannelInitialized = true
  if (typeof BroadcastChannel === 'undefined') return null
  _lockChannel = new BroadcastChannel('remy-element-locks')
  _lockChannel.addEventListener('message', (e: MessageEvent) => {
    const data = e.data || {}
    if (data.type === 'lock-request' && data.tabId !== TAB_ID) {
      const existing = heldLocks.get(data.selector)
      const granted = !existing || existing.tabId === data.tabId
      _lockChannel!.postMessage({
        type: 'lock-response',
        msgId: data.msgId,
        granted,
        holder: existing?.tabId || null,
      })
    }
  })
  return _lockChannel
}

function registerBeforeUnload() {
  if (_beforeUnloadRegistered || typeof window === 'undefined') return
  _beforeUnloadRegistered = true
  window.addEventListener('beforeunload', () => {
    const channel = getLockChannel()
    if (!channel) return
    for (const [selector] of heldLocks) {
      channel.postMessage({
        type: 'lock-release',
        selector,
        tabId: TAB_ID,
      })
    }
    heldLocks.clear()
  })
}

async function acquireElementLock(selector: string, timeout = 5000): Promise<boolean> {
  const channel = getLockChannel()
  if (!channel) return true
  registerBeforeUnload()

  return new Promise<boolean>(resolve => {
    const msgId = `${TAB_ID}:${Date.now()}:${Math.random().toString(36).slice(2)}`
    let resolved = false
    let timer: ReturnType<typeof setTimeout> | null = null

    function cleanup() {
      if (timer) clearTimeout(timer)
      channel?.removeEventListener('message', handleMessage)
      resolved = true
    }

    function handleMessage(e: MessageEvent) {
      if (resolved) return
      const data = e.data || {}
      if (data.type === 'lock-response' && data.msgId === msgId) {
        if (data.granted) {
          heldLocks.set(selector, { selector, tabId: TAB_ID, acquiredAt: Date.now() })
        }
        cleanup()
        resolve(data.granted === true)
      }
    }

    channel.addEventListener('message', handleMessage)

    channel.postMessage({
      type: 'lock-request',
      msgId,
      selector,
      tabId: TAB_ID,
    })

    timer = setTimeout(() => {
      if (!resolved) {
        resolved = true
        resolve(false)
      }
    }, timeout)
  })
}

function releaseElementLock(selector: string) {
  const channel = getLockChannel()
  if (!channel) return
  heldLocks.delete(selector)
  channel.postMessage({
    type: 'lock-release',
    selector,
    tabId: TAB_ID,
  })
}

function releaseAllLocks() {
  const channel = getLockChannel()
  if (!channel) return
  for (const [selector] of heldLocks) {
    channel.postMessage({
      type: 'lock-release',
      selector,
      tabId: TAB_ID,
    })
  }
  heldLocks.clear()
}

export interface UiCommand {
  id: string
  name: string
  args: Record<string, unknown>
}

export interface UiCommandResult {
  id: string
  name: string
  success: boolean
  result?: Record<string, unknown>
  error?: string
}

const _abortControllers = new Set<AbortController>()
const _navHistory: string[] = []
let _actionSpeed: string = 'normal'
let _paused = false
let _resumeResolver: (() => void) | null = null

const HIGHLIGHT_OUTLINE = '2px solid #3b82f6'
const HIGHLIGHT_BG = 'rgba(59, 130, 246, 0.1)'

export function abortUiCommands() {
  releaseAllLocks()
  _paused = false
  if (_resumeResolver) {
    _resumeResolver()
    _resumeResolver = null
  }
  for (const ac of _abortControllers) ac.abort()
  _abortControllers.clear()
}

export function setActionSpeed(speed: string) {
  _actionSpeed = speed
}

export function pauseUiCommands() {
  _paused = true
}

export function resumeUiCommands() {
  _paused = false
  if (_resumeResolver) {
    _resumeResolver()
    _resumeResolver = null
  }
}

export function isPaused(): boolean {
  return _paused
}

const PER_COMMAND_TIMEOUT_MS = 30000

export async function executeCommandBatch(commands: UiCommand[]): Promise<UiCommandResult[]> {
  const abort = new AbortController()
  _abortControllers.add(abort)
  const results: UiCommandResult[] = []

  const cleanup = () => {
    _abortControllers.delete(abort)
  }

  for (const cmd of commands) {
    if (abort.signal.aborted) {
      results.push({ id: cmd.id, name: cmd.name, success: false, error: 'cancelled_by_user' })
      continue
    }

    if (document.visibilityState === 'hidden') {
      await new Promise<void>(resolve => {
        const handler = () => {
          if (document.visibilityState === 'visible') {
            document.removeEventListener('visibilitychange', handler)
            resolve()
          }
        }
        document.addEventListener('visibilitychange', handler)
        setTimeout(() => {
          document.removeEventListener('visibilitychange', handler)
          if (!abort.signal.aborted) resolve()
        }, 60000)
      })
      if (abort.signal.aborted) {
        results.push({ id: cmd.id, name: cmd.name, success: false, error: 'cancelled_by_user' })
        continue
      }
    }

    if (_paused) {
      await new Promise<void>(resolve => {
        _resumeResolver = resolve
      })
      if (abort.signal.aborted) {
        results.push({ id: cmd.id, name: cmd.name, success: false, error: 'cancelled_by_user' })
        continue
      }
    }

    let result = await executeWithTimeout(cmd, abort.signal)
    if (!result.success && cmd.name === 'navigate') {
      await new Promise(r => setTimeout(r, 1000))
      result = await executeWithTimeout(cmd, abort.signal)
    }
    results.push(result)

    const speedDelays: Record<string, number> = {
      lightning: 0,
      normal: 600,
      review: 0,
    }
    const delay = speedDelays[_actionSpeed] ?? 600
    if (delay > 0) await new Promise(r => setTimeout(r, delay))
    if (_actionSpeed === 'review') _paused = true
  }

  cleanup()
  return results
}

async function executeWithTimeout(cmd: UiCommand, signal: AbortSignal): Promise<UiCommandResult> {
  return new Promise<UiCommandResult>(resolve => {
    let resolved = false
    const timer = setTimeout(() => {
      if (resolved) return
      resolved = true
      resolve({ id: cmd.id, name: cmd.name, success: false, error: 'command_timeout' })
    }, PER_COMMAND_TIMEOUT_MS)

    const onAbort = () => {
      if (resolved) return
      resolved = true
      clearTimeout(timer)
      resolve({ id: cmd.id, name: cmd.name, success: false, error: 'cancelled_by_user' })
    }

    if (signal.aborted) {
      onAbort()
      return
    }
    signal.addEventListener('abort', onAbort, { once: true })

    executeSingle(cmd).then(result => {
      if (resolved) return
      resolved = true
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      resolve(result)
    })
  })
}

async function executeSingle(cmd: UiCommand): Promise<UiCommandResult> {
  try {
    switch (cmd.name) {
      case 'navigate':
        return await navigate(cmd.args.path as string)
      case 'click':
        return await click(cmd.args.selector as string)
      case 'fill':
        return await fill(cmd.args.selector as string, cmd.args.value as string)
      case 'select':
        return await select(cmd.args.selector as string, cmd.args.value as string)
      case 'extract':
        return await doExtract(cmd.args.selector as string)
      case 'extract_all':
        return await extractAll(cmd.args.selector as string)
      case 'get_page_interactables':
        return await getPageInteractables()
      case 'wait':
        return await doWait(cmd.args)
      case 'go_back':
        return await goBack()
      case 'get_url':
        return { id: cmd.id, name: cmd.name, success: true, result: { url: location.href } }
      case 'press':
        return await pressKey(cmd.args.key as string)
      case 'spotlight': {
        const testId = cmd.args?.target as string
        const msg = cmd.args?.message as string | undefined
        if (testId) {
          spotlight.highlight(testId, msg)
        } else {
          spotlight.dismiss()
        }
        return { id: cmd.id, name: 'spotlight', success: true }
      }
      default:
        return { id: cmd.id, name: cmd.name, success: false, error: `Unknown command: ${cmd.name}` }
    }
  } catch (e) {
    return { id: cmd.id, name: cmd.name, success: false, error: formatApiError(e) }
  }
}

async function navigate(path: string): Promise<UiCommandResult> {
  const prevUrl = location.pathname + location.search
  try {
    await router.push(path)
    await waitForDomStable()
    _navHistory.push(prevUrl)
    return { id: `nav-${Date.now()}`, name: 'navigate', success: true, result: { url: location.href } }
  } catch (e) {
    return { id: `nav-${Date.now()}`, name: 'navigate', success: false, error: formatApiError(e) }
  }
}

async function click(selector: string): Promise<UiCommandResult> {
  if (!(await acquireElementLock(selector))) {
    return { id: `click-${Date.now()}`, name: 'click', success: false, error: `Could not acquire lock for element: ${selector}` }
  }
  try {
    const el = resolveElement(selector)
    if (!el) {
      return { id: `click-${Date.now()}`, name: 'click', success: false, error: `Element not found: ${selector}` }
    }
    highlightElement(el)
    const isCombobox = el.getAttribute('role') === 'combobox'
    if (isCombobox) {
      (el as HTMLElement).click()
      await new Promise(r => setTimeout(r, 300))
    } else {
      (el as HTMLElement).click()
    }
    return { id: `click-${Date.now()}`, name: 'click', success: true }
  } finally {
    releaseElementLock(selector)
  }
}

async function fill(selector: string, value: string): Promise<UiCommandResult> {
  if (!(await acquireElementLock(selector))) {
    return { id: `fill-${Date.now()}`, name: 'fill', success: false, error: `Could not acquire lock for element: ${selector}` }
  }
  try {
    const el = resolveElement(selector)
    if (!el) {
      return { id: `fill-${Date.now()}`, name: 'fill', success: false, error: `Element not found: ${selector}` }
    }
    highlightElement(el)

    const role = el.getAttribute('role')
    const tag = el.tagName.toLowerCase()

    if (role === 'combobox' || el.closest('[data-shadcn-select]') || el.closest('[role="listbox"]')) {
      (el as HTMLElement).click()
      await new Promise(r => setTimeout(r, 300))
      const commandInput = el.querySelector<HTMLInputElement>('[role="combobox"] input, [data-shadcn-command-input]')
      if (commandInput) {
        commandInput.value = value
        commandInput.dispatchEvent(new Event('input', { bubbles: true }))
        commandInput.dispatchEvent(new Event('change', { bubbles: true }))
      } else {
        const globalInput = document.querySelector<HTMLInputElement>('[role="combobox"] input, [data-shadcn-command-input]')
        if (globalInput) {
          globalInput.value = value
          globalInput.dispatchEvent(new Event('input', { bubbles: true }))
          globalInput.dispatchEvent(new Event('change', { bubbles: true }))
        }
      }
      return { id: `fill-${Date.now()}`, name: 'fill', success: true }
    }

    if (role === 'switch') {
      (el as HTMLElement).click()
      return { id: `fill-${Date.now()}`, name: 'fill', success: true }
    }

    if (el.getAttribute('contenteditable') === 'true') {
      el.textContent = value
      el.dispatchEvent(new Event('input', { bubbles: true }))
      return { id: `fill-${Date.now()}`, name: 'fill', success: true }
    }

    if (tag === 'input' || tag === 'textarea') {
      const input = el as HTMLInputElement
      const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
      )?.set
      if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, value)
      } else {
        input.value = value
      }
      input.dispatchEvent(new Event('input', { bubbles: true }))
      input.dispatchEvent(new Event('change', { bubbles: true }))
      return { id: `fill-${Date.now()}`, name: 'fill', success: true }
    }

    return { id: `fill-${Date.now()}`, name: 'fill', success: false, error: `Unsupported element: ${tag}` }
  } finally {
    releaseElementLock(selector)
  }
}

async function select(selector: string, value: string): Promise<UiCommandResult> {
  if (!(await acquireElementLock(selector))) {
    return { id: `select-${Date.now()}`, name: 'select', success: false, error: `Could not acquire lock for element: ${selector}` }
  }
  try {
    const el = resolveElement(selector)
    if (!el) {
      return { id: `select-${Date.now()}`, name: 'select', success: false, error: `Element not found: ${selector}` }
    }
    highlightElement(el)

    const option = el.querySelector(`[data-value="${CSS.escape(value)}"]`) as HTMLElement
    if (option) {
      option.click()
      return { id: `select-${Date.now()}`, name: 'select', success: true }
    }

    const nativeSelect = el as HTMLSelectElement
    if (nativeSelect.tagName === 'SELECT') {
      for (let i = 0; i < nativeSelect.options.length; i++) {
        if (nativeSelect.options[i].value === value || nativeSelect.options[i].text === value) {
          nativeSelect.selectedIndex = i
          nativeSelect.dispatchEvent(new Event('change', { bubbles: true }))
          return { id: `select-${Date.now()}`, name: 'select', success: true }
        }
      }
      return { id: `select-${Date.now()}`, name: 'select', success: false, error: `Option not found: ${value}` }
    }

    return { id: `select-${Date.now()}`, name: 'select', success: false, error: `Unsupported element for select: ${el.tagName}` }
  } finally {
    releaseElementLock(selector)
  }
}

async function doExtract(selector: string): Promise<UiCommandResult> {
  const el = resolveElement(selector)
  if (!el) {
    return { id: `extract-${Date.now()}`, name: 'extract', success: false, error: `Element not found: ${selector}` }
  }
  const text = sanitizeExtract(el)
  return { id: `extract-${Date.now()}`, name: 'extract', success: true, result: { text, selector } }
}

async function extractAll(selector: string): Promise<UiCommandResult> {
  const elements = document.querySelectorAll(selector)
  const results: Array<{ index: number; text: string; selector: string }> = []
  elements.forEach((el, i) => {
    results.push({ index: i, text: sanitizeExtract(el), selector })
  })
  return { id: `extract-all-${Date.now()}`, name: 'extract_all', success: true, result: { items: results, count: results.length } }
}

async function getPageInteractables(): Promise<UiCommandResult> {
  const interactables: Array<Record<string, unknown>> = []
  const selector = 'button, a, input, select, textarea, [data-testid], [role="button"], [role="checkbox"], [role="switch"]'
  document.querySelectorAll(selector).forEach(el => {
    const htmlEl = el as HTMLElement
    if (!htmlEl.offsetParent && !htmlEl.offsetWidth && !htmlEl.offsetHeight) return
    const tag = el.tagName.toLowerCase()
    const testid = el.getAttribute('data-testid')
    const sel = buildSelector(el)
    if (!sel) return
    interactables.push({
      tag,
      type: el.getAttribute('type') || undefined,
      text: (el.textContent || '').trim().slice(0, 100) || undefined,
      testid: testid || undefined,
      name: el.getAttribute('name') || undefined,
      id: el.getAttribute('id') || undefined,
      selector: sel,
      disabled: (htmlEl as HTMLButtonElement | HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement).disabled || false,
      visible: true,
    })
  })
  return { id: `interactables-${Date.now()}`, name: 'get_page_interactables', success: true, result: { items: interactables, count: interactables.length } }
}

async function doWait(args: Record<string, unknown>): Promise<UiCommandResult> {
  if (args.selector) {
    const timeout = (args.timeout as number) ?? 10000
    const start = Date.now()
    while (Date.now() - start < timeout) {
      const el = resolveElement(args.selector as string)
      if (el) {
        return { id: `wait-${Date.now()}`, name: 'wait', success: true, result: { found: true, selector: args.selector } }
      }
      await new Promise(r => requestAnimationFrame(r))
    }
    return { id: `wait-${Date.now()}`, name: 'wait', success: false, error: `Timeout waiting for: ${args.selector}` }
  }
  if (args.ms) {
    await new Promise(r => setTimeout(r, args.ms as number))
    return { id: `wait-${Date.now()}`, name: 'wait', success: true }
  }
  return { id: `wait-${Date.now()}`, name: 'wait', success: true }
}

async function goBack(): Promise<UiCommandResult> {
  const prev = _navHistory.pop()
  if (prev) {
    await router.push(prev)
    await waitForDomStable()
    return { id: `back-${Date.now()}`, name: 'go_back', success: true, result: { url: location.href } }
  }
  return { id: `back-${Date.now()}`, name: 'go_back', success: false, error: 'No navigation history' }
}

async function pressKey(key: string): Promise<UiCommandResult> {
  const target = document.activeElement || document.body
  target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }))
  target.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true, cancelable: true }))
  return { id: `press-${Date.now()}`, name: 'press', success: true }
}

function buildSelector(el: Element): string | null {
  if (el.getAttribute('data-testid')) {
    return `[data-testid="${CSS.escape(el.getAttribute('data-testid')!)}"]`
  }
  if (el.getAttribute('id')) {
    return `#${CSS.escape(el.getAttribute('id')!)}`
  }
  const tag = el.tagName.toLowerCase()
  const text = (el.textContent || '').trim().slice(0, 50)
  if (text) {
    const parent = el.parentElement
    const sameTagSiblings = parent ? Array.from(parent.children).filter(c => c.tagName === el.tagName) : [el]
    const nth = 1 + sameTagSiblings.indexOf(el)
    return `${tag}:nth-of-type(${nth})`
  }
  return null
}

function resolveElement(selector: string): Element | null {
  if (!selector.startsWith('[') && !selector.startsWith('.') && !selector.startsWith('#')) {
    const testid = `[data-testid="${CSS.escape(selector)}"]`
    const byTestId = document.querySelector(testid)
    if (byTestId) return byTestId
  }
  return document.querySelector(selector)
}

function highlightElement(el: Element, duration = 500) {
  const htmlEl = el as HTMLElement
  const origOutline = htmlEl.style.outline
  const origBg = htmlEl.style.backgroundColor
  htmlEl.style.outline = HIGHLIGHT_OUTLINE
  htmlEl.style.outlineOffset = '2px'
  htmlEl.style.backgroundColor = HIGHLIGHT_BG
  setTimeout(() => {
    htmlEl.style.outline = origOutline
    htmlEl.style.backgroundColor = origBg
  }, duration)
}

function sanitizeExtract(el: Element): string {
  const clone = el.cloneNode(true) as Element
  clone.querySelectorAll('script, style, noscript, template, input[type="hidden"]').forEach(n => n.remove())
  clone.querySelectorAll<HTMLInputElement>('input[type="password"]').forEach(n => {
    n.value = '••••••••'
  })
  return clone.textContent?.trim() || ''
}

export function waitForDomStable(timeout = 10000): Promise<void> {
  return new Promise((resolve) => {
    const scope = document.querySelector('main') || document.querySelector('[role="main"]') || document.body
    let timer: ReturnType<typeof setTimeout> | null = null
    let resolved = false

    const checkSpinners = () => {
      const spinners = scope.querySelectorAll<HTMLElement>('[aria-busy="true"], .loading, .spinner, [data-loading="true"]')
      return spinners.length > 0
    }

    const done = () => {
      if (resolved) return
      resolved = true
      observer.disconnect()
      resolve()
    }

    const observer = new MutationObserver(() => {
      if (resolved) return
      if (checkSpinners()) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            done()
          })
        })
      }, 200)
    })

    observer.observe(scope, { childList: true, subtree: true, characterData: true })

    if (!checkSpinners()) {
      timer = setTimeout(() => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            done()
          })
        })
      }, 200)
    }

    setTimeout(() => {
      done()
    }, timeout)
  })
}
