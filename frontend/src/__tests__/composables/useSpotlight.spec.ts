import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// jsdom does not implement CSS.escape; polyfill the subset the composable uses.
const cssEscape = (value: string): string =>
  String(value).replace(/[^a-zA-Z0-9_\u00A0-\uFFFF-]/g, (ch) => {
    if (ch === '\u0000') return '\uFFFD'
    if (ch.charCodeAt(0) < 0x20) return `\\${ch.codePointAt(0)!.toString(16)} `
    return `\\${ch}`
  })

if (typeof globalThis.CSS === 'undefined') {
  ;(globalThis as { CSS?: { escape: (value: string) => string } }).CSS = { escape: cssEscape }
} else if (typeof (globalThis as { CSS: { escape?: unknown } }).CSS.escape !== 'function') {
  ;(globalThis as { CSS: { escape: (value: string) => string } }).CSS.escape = cssEscape
}

beforeEach(() => {
  vi.resetModules()
  document.body.innerHTML = ''
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('useSpotlight', () => {
  it('starts inactive with no target or message', async () => {
    const { useSpotlight } = await import('../../composables/useSpotlight')
    const spotlight = useSpotlight()
    expect(spotlight.active.value).toBe(false)
    expect(spotlight.target.value).toBeNull()
    expect(spotlight.message.value).toBeNull()
    expect(spotlight.targetElement.value).toBeNull()
  })

  it('highlight activates the target without a message by default', async () => {
    const { useSpotlight } = await import('../../composables/useSpotlight')
    const spotlight = useSpotlight()
    spotlight.highlight('run-list')
    expect(spotlight.active.value).toBe(true)
    expect(spotlight.target.value).toBe('run-list')
    expect(spotlight.message.value).toBeNull()
  })

  it('highlight stores the optional message', async () => {
    const { useSpotlight } = await import('../../composables/useSpotlight')
    const spotlight = useSpotlight()
    spotlight.highlight('run-list', 'Click a run to inspect it')
    expect(spotlight.message.value).toBe('Click a run to inspect it')
  })

  it('a second highlight replaces the previous target and message', async () => {
    const { useSpotlight } = await import('../../composables/useSpotlight')
    const spotlight = useSpotlight()
    spotlight.highlight('run-list', 'first')
    spotlight.highlight('pipeline-list')
    expect(spotlight.target.value).toBe('pipeline-list')
    expect(spotlight.message.value).toBeNull()
  })

  it('dismiss clears the active state', async () => {
    const { useSpotlight } = await import('../../composables/useSpotlight')
    const spotlight = useSpotlight()
    spotlight.highlight('run-list', 'msg')
    spotlight.dismiss()
    expect(spotlight.active.value).toBe(false)
    expect(spotlight.target.value).toBeNull()
    expect(spotlight.message.value).toBeNull()
  })

  it('dismiss on an already-inactive spotlight is a no-op', async () => {
    const { useSpotlight } = await import('../../composables/useSpotlight')
    const spotlight = useSpotlight()
    spotlight.dismiss()
    expect(spotlight.active.value).toBe(false)
    expect(spotlight.target.value).toBeNull()
  })

  it('targetElement resolves the element by data-testid', async () => {
    const { useSpotlight } = await import('../../composables/useSpotlight')
    const spotlight = useSpotlight()
    const el = document.createElement('button')
    el.dataset.testid = 'run-list'
    document.body.appendChild(el)

    spotlight.highlight('run-list')
    expect(spotlight.targetElement.value).toBe(el)
  })

  it('targetElement is null when the highlighted element does not exist', async () => {
    const { useSpotlight } = await import('../../composables/useSpotlight')
    const spotlight = useSpotlight()
    spotlight.highlight('missing-element')
    expect(spotlight.targetElement.value).toBeNull()
  })

  it('targetElement escapes special characters in the test id', async () => {
    const { useSpotlight } = await import('../../composables/useSpotlight')
    const spotlight = useSpotlight()
    const el = document.createElement('div')
    el.dataset.testid = 'settings:run#1'
    document.body.appendChild(el)

    spotlight.highlight('settings:run#1')
    expect(spotlight.targetElement.value).toBe(el)
  })

  it('is a module-level singleton shared across consumers', async () => {
    const mod = await import('../../composables/useSpotlight')
    const spotlight = mod.useSpotlight()
    expect(spotlight.target.value).toBe(mod.spotlight.target.value)
    mod.spotlight.highlight('shared-target', 'shared-msg')
    expect(spotlight.target.value).toBe('shared-target')
    expect(spotlight.message.value).toBe('shared-msg')
    expect(spotlight.active.value).toBe(true)
  })
})
