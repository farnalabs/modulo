import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import CommandPalette from '../components/CommandPalette.vue'

beforeEach(() => {
  // CommandPalette uses useNavVisibilityContext, which depends on the plan
  // Pinia store, so a Pinia instance must be active during mount.
  setActivePinia(createPinia())
  // jsdom does not implement scrollIntoView; the selectedIndex watcher calls
  // it whenever the selection changes.
  Element.prototype.scrollIntoView = vi.fn()
})

async function openPalette() {
  const wrapper = mount(CommandPalette, { attachTo: document.body })
  ;(wrapper.vm as unknown as { open: () => void }).open()
  await nextTick()
  await nextTick()
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
})
