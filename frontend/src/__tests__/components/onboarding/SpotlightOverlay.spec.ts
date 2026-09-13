import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import SpotlightOverlay from '../../../components/onboarding/SpotlightOverlay.vue'
import { spotlight } from '../../../composables/useSpotlight'

const TARGET_TESTID = 'spotlight-target'

describe('SpotlightOverlay — keyboard dismissal', () => {
  beforeEach(() => {
    document.body.innerHTML = `<button data-testid="${TARGET_TESTID}">target</button>`
  })

  afterEach(() => {
    spotlight.dismiss()
    document.body.innerHTML = ''
  })

  it('renders only while a target is highlighted', async () => {
    const wrapper = mount(SpotlightOverlay, { attachTo: document.body })
    expect(spotlight.active.value).toBe(false)
    expect(wrapper.find('[data-testid="spotlight-overlay"]').exists()).toBe(false)

    spotlight.highlight(TARGET_TESTID, 'focus the target')
    await flushPromises()
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(spotlight.active.value).toBe(true)
    expect(wrapper.find('[data-testid="spotlight-overlay"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('dismisses on Escape via the document-level keydown listener', async () => {
    const wrapper = mount(SpotlightOverlay, { attachTo: document.body })
    spotlight.highlight(TARGET_TESTID, 'focus the target')
    await flushPromises()
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(spotlight.active.value).toBe(true)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    await wrapper.vm.$nextTick()

    expect(spotlight.active.value).toBe(false)
    wrapper.unmount()
  })

  it('does not dismiss on Escape after unmount (listener removed)', async () => {
    const wrapper = mount(SpotlightOverlay, { attachTo: document.body })
    spotlight.highlight(TARGET_TESTID, 'focus the target')
    await flushPromises()
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    wrapper.unmount()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(spotlight.active.value).toBe(true)
  })

  it('stops Enter/Space propagation on the cutout so they do not bubble to the overlay', async () => {
    const wrapper = mount(SpotlightOverlay, { attachTo: document.body })
    spotlight.highlight(TARGET_TESTID, 'focus the target')
    await flushPromises()
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    const overlay = wrapper.find('[data-testid="spotlight-overlay"]')
    const cutout = overlay.element.firstElementChild as HTMLElement
    expect(cutout.className).toContain('border-primary')

    let bubbled = false
    overlay.element.addEventListener('keydown', () => {
      bubbled = true
    })

    cutout.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(bubbled).toBe(false)

    cutout.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }))
    expect(bubbled).toBe(false)

    wrapper.unmount()
  })
})
