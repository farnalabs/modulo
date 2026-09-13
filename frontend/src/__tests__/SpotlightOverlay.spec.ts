import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
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

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverMock)
  spotlight.dismiss()
})

afterEach(() => {
  spotlight.dismiss()
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('SpotlightOverlay', () => {
  it('renders the overlay when the spotlight is active', () => {
    targetEl('spot-target')
    spotlight.highlight('spot-target', 'do the thing')
    const wrapper = mount(SpotlightOverlay, mountOpts)
    expect(overlayEl()).not.toBeNull()
    expect(wrapper.text()).toContain('do the thing')
  })

  it('dismisses when Escape is pressed on the overlay backdrop', async () => {
    targetEl('spot-target')
    spotlight.highlight('spot-target')
    mount(SpotlightOverlay, mountOpts)
    expect(overlayEl()).not.toBeNull()

    overlayEl()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await Promise.resolve()

    expect(overlayEl()).toBeNull()
  })

  it('dismisses when Escape is pressed at the document level', async () => {
    targetEl('spot-target')
    spotlight.highlight('spot-target')
    mount(SpotlightOverlay, mountOpts)
    expect(overlayEl()).not.toBeNull()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await Promise.resolve()

    expect(overlayEl()).toBeNull()
  })

  it('exposes keyboard handlers (enter/space) on the cutout without dismissing', async () => {
    targetEl('spot-target')
    spotlight.highlight('spot-target')
    mount(SpotlightOverlay, mountOpts)
    const cutout = document.querySelector('[data-testid="spotlight-overlay"] > div') as HTMLElement | null
    expect(cutout).not.toBeNull()

    // These bindings must not throw and must stop propagation (not dismiss).
    cutout!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    cutout!.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }))
    await Promise.resolve()

    // Enter/Space on the cutout are not dismiss actions, so the overlay stays.
    expect(overlayEl()).not.toBeNull()
  })

  it('removes the document keydown listener on unmount', () => {
    const removeSpy = vi.spyOn(document, 'removeEventListener')
    targetEl('spot-target')
    spotlight.highlight('spot-target')
    const wrapper = mount(SpotlightOverlay, mountOpts)
    expect(overlayEl()).not.toBeNull()

    wrapper.unmount()
    // onUnmounted must detach the document-level keydown listener so a later
    // Escape press cannot dismiss a spotlight that is no longer mounted.
    expect(removeSpy).toHaveBeenCalledWith('keydown', expect.any(Function))
    removeSpy.mockRestore()
  })

  it('does not dismiss on Escape after unmount (listener removed)', async () => {
    targetEl('spot-target')
    spotlight.highlight('spot-target', 'focus the target')
    const wrapper = mount(SpotlightOverlay, mountOpts)
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
    spotlight.highlight('spot-target', 'focus the target')
    const wrapper = mount(SpotlightOverlay, mountOpts)
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
