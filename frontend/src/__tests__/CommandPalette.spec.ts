import { mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import CommandPalette from '../components/CommandPalette.vue'

// Track mounted wrappers so each test tears down cleanly. CommandPalette
// registers a document-level keydown listener in onMounted; clearing
// document.body alone leaves the listener attached to a detached instance,
// which would throw when a later test dispatches a document keydown.
const mounted: VueWrapper[] = []

beforeEach(() => {
  // CommandPalette uses useNavVisibilityContext, which depends on the plan
  // Pinia store, so a Pinia instance must be active during mount.
  setActivePinia(createPinia())
  // jsdom does not implement scrollIntoView; the selectedIndex watcher calls
  // it whenever the selection changes.
  Element.prototype.scrollIntoView = vi.fn()
})

// Track every mounted palette so afterEach can unmount them. The component
// registers a document-level keydown listener in onMounted; without an explicit
// unmount that listener leaks across tests and fires on unrelated Escape
// dispatches, crashing the suite.
const mountedPalettes: Array<ReturnType<typeof mount>> = []

async function openPalette() {
  const wrapper = mount(CommandPalette, { attachTo: document.body })
  mounted.push(wrapper)
  ;(wrapper.vm as unknown as { open: () => void }).open()
  await nextTick()
  await nextTick()
  mountedPalettes.push(wrapper)
  return wrapper
}

function paletteInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>('input[placeholder="Search pages..."]')
  expect(input).not.toBeNull()
  return input!
}

function resultButtons(): HTMLElement[] {
  return Array.from(document.querySelectorAll('[data-cmdk-container] button')) as HTMLElement[]
}

function highlightedButton(): HTMLElement | null {
  return resultButtons().find(b => b.classList.contains('bg-accent')) ?? null
}

async function typeQuery(value: string) {
  const input = paletteInput()
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  await nextTick()
}

describe('CommandPalette', () => {
  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount()
    document.body.innerHTML = ''
  })

  it('clamps selectedIndex when the query shrinks the result list', async () => {
    await openPalette()
    expect(resultButtons().length).toBeGreaterThan(3)

    // Move the selection into the middle of the full list.
    const input = paletteInput()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    const beforeShrink = highlightedButton()
    expect(beforeShrink).not.toBeNull()

    // Shrink the list to a single result far above the current selection.
    await typeQuery('ab test')
    const shrunk = resultButtons()
    expect(shrunk).toHaveLength(1)
    const highlighted = highlightedButton()
    // Regression: without the clamping watcher the selection stays out of
    // range (index 2 of a 1-item list) and nothing is highlighted.
    expect(highlighted).not.toBeNull()
    expect(highlighted!.textContent).toContain('AB Test Models')
  })

  it('resets selection to 0 when the filtered list empties', async () => {
    await openPalette()
    const input = paletteInput()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()

    // Empty the list.
    await typeQuery('zzzz-no-such-page')
    expect(resultButtons()).toHaveLength(0)

    // Clearing the query restores the full list; selection must be back at 0
    // (before the fix it stayed at index 2 and highlighted the 3rd item).
    await typeQuery('')
    const highlighted = highlightedButton()
    expect(highlighted).not.toBeNull()
    expect(resultButtons().indexOf(highlighted!)).toBe(0)
  })

  it('closes on Escape via the document-level keydown handler', async () => {
    await openPalette()
    expect(paletteInput()).not.toBeNull()

    // The new Escape branch in handleKeydown must close the palette.
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    // With isOpen cleared the dialog (and its search input) is unmounted.
    expect(document.querySelector('input[placeholder="Search pages..."]')).toBeNull()
  })

  it('closes on Escape via the overlay backdrop keydown handler', async () => {
    await openPalette()
    expect(paletteInput()).not.toBeNull()

    // The template @keydown.escape="close" binding on the backdrop overlay.
    const backdrop = document.querySelector('.fixed.inset-0.bg-black\\/50') as HTMLElement | null
    expect(backdrop).not.toBeNull()
    backdrop!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    expect(document.querySelector('input[placeholder="Search pages..."]')).toBeNull()
  })
})
