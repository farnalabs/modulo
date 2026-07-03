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

let currentAbort: AbortController | null = null
const _navHistory: string[] = []

export function abortUiCommands() {
  currentAbort?.abort()
}

export async function executeCommandBatch(commands: UiCommand[]): Promise<UiCommandResult[]> {
  const abort = new AbortController()
  currentAbort = abort
  const results: UiCommandResult[] = []

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

    const result = await executeSingle(cmd)
    results.push(result)
  }

  return results
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
      default:
        return { id: cmd.id, name: cmd.name, success: false, error: `Unknown command: ${cmd.name}` }
    }
  } catch (e) {
    return { id: cmd.id, name: cmd.name, success: false, error: String(e) }
  }
}

async function navigate(path: string): Promise<UiCommandResult> {
  const { useRouter } = await import('vue-router')
  const router = useRouter()
  _navHistory.push(location.pathname + location.search)
  router.push(path)
  await waitForDomStable()
  return { id: `nav-${Date.now()}`, name: 'navigate', success: true, result: { url: location.href } }
}

async function click(selector: string): Promise<UiCommandResult> {
  const el = resolveElement(selector)
  if (!el) {
    return { id: `click-${Date.now()}`, name: 'click', success: false, error: `Element not found: ${selector}` }
  }
  highlightElement(el)
  const isCombobox = el.getAttribute('role') === 'combobox'
  if (isCombobox) {
    ;(el as HTMLElement).click()
    await new Promise(r => setTimeout(r, 300))
  } else {
    ;(el as HTMLElement).click()
  }
  return { id: `click-${Date.now()}`, name: 'click', success: true }
}

async function fill(selector: string, value: string): Promise<UiCommandResult> {
  const el = resolveElement(selector)
  if (!el) {
    return { id: `fill-${Date.now()}`, name: 'fill', success: false, error: `Element not found: ${selector}` }
  }
  highlightElement(el)

  const role = el.getAttribute('role')
  const tag = el.tagName.toLowerCase()

  if (role === 'combobox' || el.closest('[data-shadcn-select]') || el.closest('[role="listbox"]')) {
    ;(el as HTMLElement).click()
    await new Promise(r => setTimeout(r, 300))
    const commandInput = document.querySelector<HTMLInputElement>('[role="combobox"] input, [data-shadcn-command-input]')
    if (commandInput) {
      commandInput.value = value
      commandInput.dispatchEvent(new Event('input', { bubbles: true }))
      commandInput.dispatchEvent(new Event('change', { bubbles: true }))
    }
    return { id: `fill-${Date.now()}`, name: 'fill', success: true }
  }

  if (role === 'switch' || el.getAttribute('aria-role') === 'switch') {
    ;(el as HTMLElement).click()
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
}

async function select(selector: string, value: string): Promise<UiCommandResult> {
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
  const { useRouter } = await import('vue-router')
  const router = useRouter()
  const prev = _navHistory.pop()
  if (prev) {
    router.push(prev)
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
    const escaped = CSS.escape(text)
    return `${tag}:contains("${escaped}")`
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
  htmlEl.style.outline = '2px solid #3b82f6'
  htmlEl.style.outlineOffset = '2px'
  htmlEl.style.backgroundColor = 'rgba(59, 130, 246, 0.1)'
  setTimeout(() => {
    htmlEl.style.outline = origOutline
    htmlEl.style.backgroundColor = origBg
  }, duration)
}

function sanitizeExtract(el: Element): string {
  const clone = el.cloneNode(true) as Element
  clone.querySelectorAll('script, style, noscript, template, [type="hidden"]').forEach(n => n.remove())
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
