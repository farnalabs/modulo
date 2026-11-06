import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import SpotlightOverlay from '../components/onboarding/SpotlightOverlay.vue'
import { spotlight } from '../composables/useSpotlight'

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      components: {
        onboarding: {
          SpotlightOverlay: {
            dismiss_hint: 'Press Esc to dismiss',
          },
        },
      },
    },
  },
})

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function targetEl(testid: string): HTMLElement {
  const el = document.createElement('div')
  el.setAttribute('data-testid', testid)
  document.body.appendChild(el)
  return el
}

function overlayEl(): HTMLElement | null {
  return document.querySelector('[data-testid="spotlight-overlay"]')
}

const mountOpts = { global: { plugins: [i18n] }, attachTo: document.body }

// Track every mounted wrapper so we can tear them all down between tests.
// Earlier tests that mounted without keeping a reference left their
// document-level keydown listeners attached, which made the
// "does not dismiss on Escape after unmount" check flaky (a later Escape
// would still reach those stale listeners and dismiss the spotlight).
const wrappers: VueWrapper[] = []

function mountOverlay(): VueWrapper {
  const wrapper = mount(SpotlightOverlay, mountOpts)
  wrappers.push(wrapper)
  return wrapper
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverMock)
  spotlight.dismiss()
})

afterEach(() => {
  wrappers.forEach((w) => w.unmount())
  wrappers.length = 0
  spotlight.dismiss()
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('SpotlightOverlay', () => {
  it('renders the overlay when the spotlight is active', () => {
    targetEl('spot-target')
    spotlight.highlight('spot-target', 'do the thing')
    const wrapper = mountOverlay()
    expect(overlayEl()).not.toBeNull()
    expect(wrapper.text()).toContain('do the thing')
  })

  it('dismisses when Escape is pressed on the overlay backdrop', async () => {
    targetEl('spot-target')
    spotlight.highlight('spot-target')
    mountOverlay()
    expect(overlayEl()).not.toBeNull()

    overlayEl()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await Promise.resolve()

    expect(overlayEl()).toBeNull()
  })

  it('dismisses when Escape is pressed at the document level', async () => {
    targetEl('spot-target')
    spotlight.highlight('spot-target')
    mountOverlay()
    expect(overlayEl()).not.toBeNull()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await Promise.resolve()

    expect(overlayEl()).toBeNull()
  })

  it('exposes keyboard handlers (enter/space) on the cutout without dismissing', async () => {
    targetEl('spot-target')
    const wrapper = mountOverlay()
    // Highlight after mount so the rect watcher runs and the cutout branch
    // (the div that actually carries the enter/space handlers) is rendered
    // instead of the fallback branch.
    spotlight.highlight('spot-target')
    await nextTick()
    const cutout = document.querySelector('[data-testid="spotlight-overlay"] > div') as HTMLElement | null
    expect(cutout).not.toBeNull()

    // These bindings must not throw and must stop propagation (not dismiss).
    cutout!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    cutout!.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }))
    await Promise.resolve()

    // Enter/Space on the cutout are not dismiss actions, so the overlay stays.
    expect(overlayEl()).not.toBeNull()
    wrapper.unmount()
  })

  it('updates the cutout position when the target moves', async () => {
    const target = targetEl('spot-target')
    const rect = { top: 10, right: 110, bottom: 60, left: 10, width: 100, height: 50 } as DOMRect
    vi.spyOn(target, 'getBoundingClientRect').mockReturnValue(rect)
    spotlight.highlight('spot-target')
    const wrapper = mountOverlay()
    await nextTick()

    expect((document.querySelector('[data-testid="spotlight-overlay"] > div') as HTMLElement).style.left).toBe('10px')

    const movedRect = { ...rect, left: 40, right: 140 } as DOMRect
    vi.mocked(target.getBoundingClientRect).mockReturnValue(movedRect)
    window.dispatchEvent(new Event('scroll'))
    await nextTick()

    expect((document.querySelector('[data-testid="spotlight-overlay"] > div') as HTMLElement).style.left).toBe('40px')
    wrapper.unmount()
  })

  it('removes document and position listeners on unmount', () => {
    const removeDocumentSpy = vi.spyOn(document, 'removeEventListener')
    const removeWindowSpy = vi.spyOn(window, 'removeEventListener')
    targetEl('spot-target')
    spotlight.highlight('spot-target')
    const wrapper = mountOverlay()
    expect(overlayEl()).not.toBeNull()

    wrapper.unmount()

    expect(removeDocumentSpy).toHaveBeenCalledWith('keydown', expect.any(Function))
    expect(removeWindowSpy).toHaveBeenCalledWith('scroll', expect.any(Function), true)
    expect(removeWindowSpy).toHaveBeenCalledWith('resize', expect.any(Function))
    removeDocumentSpy.mockRestore()
    removeWindowSpy.mockRestore()
  })

  it('does not dismiss on Escape after unmount (listener removed)', async () => {
    targetEl('spot-target')
    spotlight.highlight('spot-target', 'focus the target')
    const wrapper = mountOverlay()
    expect(overlayEl()).not.toBeNull()

    wrapper.unmount()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await Promise.resolve()

    // The document keydown listener was removed on unmount, so the spotlight
    // stays active (Escape no longer reaches it).
    expect(spotlight.active.value).toBe(true)
  })

  it('stops Enter/Space propagation on the cutout so they do not bubble to the overlay', async () => {
    targetEl('spot-target')
    const wrapper = mountOverlay()
    // Highlight after mount so the rect watcher runs and the cutout branch
    // (which carries the .stop modifiers) renders instead of the fallback
    // branch that has no propagation guards.
    spotlight.highlight('spot-target', 'focus the target')
    await nextTick()
    const cutout = document.querySelector('[data-testid="spotlight-overlay"] > div') as HTMLElement | null
    expect(cutout).not.toBeNull()

    let bubbled = false
    const overlay = overlayEl()!
    overlay.addEventListener('keydown', () => {
      bubbled = true
    })

    cutout!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(bubbled).toBe(false)

    cutout!.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }))
    expect(bubbled).toBe(false)

    wrapper.unmount()
  })
})
