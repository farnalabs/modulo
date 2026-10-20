import { mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import CommandPalette from '../components/CommandPalette.vue'

// Track mounted wrappers so afterEach can unmount them. CommandPalette is
// mounted with attachTo: document.body, so each wrapper must be explicitly
// unmounted to detach its document-level keydown listener between tests.
const mounted: VueWrapper[] = []

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
  mounted.push(wrapper)
  await (wrapper.vm as unknown as { open: () => Promise<void> }).open()
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
    while (mounted.length) mounted.pop()?.unmount()
    document.body.innerHTML = ''
  })

  it('clamps selectedIndex when the query shrinks the result list', async () => {
    await openPalette()
    const allItems = resultButtons()
    expect(allItems.length).toBeGreaterThan(3)

    // Move the selection into the middle of the full list.
    const input = paletteInput()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    const beforeShrink = highlightedButton()
    expect(beforeShrink).not.toBeNull()

    // Shrink the list to a single result far above the current selection.
    // "Pipelines" is a curated extra in the palette that narrows to 1 hit.
    await typeQuery('pipelines')
    const shrunk = resultButtons()
    expect(shrunk).toHaveLength(1)
    const highlighted = highlightedButton()
    // Regression: without the clamping watcher the selection stays out of
    // range (index 2 of a 1-item list) and nothing is highlighted.
    expect(highlighted).not.toBeNull()
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

  it('is a genuinely modal dialog (opened with showModal, focus inside)', async () => {
    const showModal = vi.spyOn(HTMLDialogElement.prototype, 'showModal')
    await openPalette()

    const dialog = document.querySelector('dialog') as HTMLDialogElement | null
    expect(dialog).not.toBeNull()
    // showModal() is the whole point: the opaque sheet is modal, not merely
    // painted over the page while Tab can still reach it.
    expect(showModal).toHaveBeenCalledTimes(1)
    expect(dialog!.open).toBe(true)
    expect(dialog!.contains(document.activeElement)).toBe(true)
    showModal.mockRestore()
  })

  it('closes on a backdrop click', async () => {
    await openPalette()
    expect(paletteInput()).not.toBeNull()

    // A click whose target is the dialog itself is a ::backdrop click.
    const dialog = document.querySelector('dialog') as HTMLDialogElement
    dialog.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()

    expect(document.querySelector('input[placeholder="Search pages..."]')).toBeNull()
  })

  it('returns focus to the previously focused trigger when it closes', async () => {
    const trigger = document.createElement('button')
    trigger.textContent = 'Open palette'
    document.body.appendChild(trigger)
    trigger.focus()

    await openPalette()
    expect(document.activeElement).not.toBe(trigger)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('unmounts safely while the modal is open', async () => {
    const wrapper = await openPalette()
    expect((document.querySelector('dialog') as HTMLDialogElement).open).toBe(true)
    expect(() => wrapper.unmount()).not.toThrow()
    mounted.pop()
  })
})
