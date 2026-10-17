import { describe, it, expect, afterEach } from 'vitest'
import { FOCUSABLE_SELECTOR, trapTabInElement } from '../../composables/useFocusTrap'

// The single owner of the Tab-cycle algorithm and its selector. This spec
// exercises every branch directly so the composable's behaviour (including the
// container-focus and focus-escaped edges) is pinned independent of any view.

function buildRoot(inner = '<button id="first">first</button><button id="last">last</button>'): HTMLElement {
  const root = document.createElement('div')
  root.tabIndex = -1
  root.innerHTML = inner
  document.body.appendChild(root)
  return root
}

function pressTab(opts: { shiftKey?: boolean } = {}): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey: opts.shiftKey, bubbles: true, cancelable: true })
  return event
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('trapTabInElement', () => {
  it('is a no-op for a null root', () => {
    const event = pressTab()
    expect(() => trapTabInElement(event, null)).not.toThrow()
    expect(event.defaultPrevented).toBe(false)
  })

  it('preventDefaults when the root has no focusable descendants', () => {
    const root = buildRoot('plain text')
    root.focus()
    expect(document.activeElement).toBe(root)

    const event = pressTab({ shiftKey: true })
    trapTabInElement(event, root)

    expect(event.defaultPrevented).toBe(true)
    // Focus stays pinned on the container rather than escaping behind it.
    expect(document.activeElement).toBe(root)
  })

  it('wraps Shift+Tab from the first focusable to the last', () => {
    const root = buildRoot()
    const first = root.querySelector<HTMLElement>('#first')!
    const last = root.querySelector<HTMLElement>('#last')!
    first.focus()

    const event = pressTab({ shiftKey: true })
    trapTabInElement(event, root)

    expect(event.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(last)
  })

  it('wraps Shift+Tab from the container itself to the last focusable', () => {
    const root = buildRoot()
    const last = root.querySelector<HTMLElement>('#last')!
    root.focus()
    expect(document.activeElement).toBe(root)

    const event = pressTab({ shiftKey: true })
    trapTabInElement(event, root)

    expect(event.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(last)
  })

  it('wraps Shift+Tab back in when focus has escaped the root', () => {
    const root = buildRoot()
    const last = root.querySelector<HTMLElement>('#last')!
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    outside.focus()
    expect(document.activeElement).toBe(outside)

    const event = pressTab({ shiftKey: true })
    trapTabInElement(event, root)

    expect(event.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(last)
  })

  it('leaves Shift+Tab from a non-first focusable to the browser', () => {
    const root = buildRoot()
    const last = root.querySelector<HTMLElement>('#last')!
    last.focus()

    const event = pressTab({ shiftKey: true })
    trapTabInElement(event, root)

    expect(event.defaultPrevented).toBe(false)
    expect(document.activeElement).toBe(last)
  })

  it('wraps Tab from the last focusable to the first', () => {
    const root = buildRoot()
    const first = root.querySelector<HTMLElement>('#first')!
    const last = root.querySelector<HTMLElement>('#last')!
    last.focus()

    const event = pressTab()
    trapTabInElement(event, root)

    expect(event.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(first)
  })

  it('wraps Tab back in when focus has escaped the root', () => {
    const root = buildRoot()
    const first = root.querySelector<HTMLElement>('#first')!
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    outside.focus()
    expect(document.activeElement).toBe(outside)

    const event = pressTab()
    trapTabInElement(event, root)

    expect(event.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(first)
  })

  it('leaves Tab from a middle focusable to the browser', () => {
    const root = buildRoot('<button id="middle">middle</button><button id="last">last</button>')
    const middle = root.querySelector<HTMLElement>('#middle')!
    middle.focus()

    const event = pressTab()
    trapTabInElement(event, root)

    expect(event.defaultPrevented).toBe(false)
    expect(document.activeElement).toBe(middle)
  })

  it('exposes the shared focusable selector', () => {
    expect(FOCUSABLE_SELECTOR).toContain('button:not([disabled])')
    expect(FOCUSABLE_SELECTOR).toContain('[tabindex]:not([tabindex="-1"])')
  })
})
