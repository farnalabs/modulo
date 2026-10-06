import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import FlowControls from '../../components/shared/FlowControls.vue'

// Deliberately NOT mocking the controls library: this spec exists to prove the
// wrapper's icon-slot overrides reach the REAL <button> markup the library
// renders, so the sr-only names land inside the real control buttons
// (axe [critical] button-name, FAR-740).

const NAMED_BUTTONS = [
  { selector: '.vue-flow__controls-zoomin', name: 'Zoom in' },
  { selector: '.vue-flow__controls-zoomout', name: 'Zoom out' },
  { selector: '.vue-flow__controls-fitview', name: 'Fit view' },
] as const

/**
 * Accessible name for a name-from-contents button: the button's whole rendered
 * text, whitespace-normalised. sr-only text counts; an aria-hidden icon
 * contributes no text.
 */
function accessibleName(el: Element): string {
  return (el.textContent ?? '').replace(/\s+/g, ' ').trim()
}

describe('FlowControls', () => {
  it('names each control button with sr-only text (axe button-name)', () => {
    const wrapper = mount(FlowControls)

    expect(wrapper.findAll('button')).toHaveLength(NAMED_BUTTONS.length)

    for (const { selector, name } of NAMED_BUTTONS) {
      const button = wrapper.find(selector)
      expect(button.exists()).toBe(true)
      expect(button.element.tagName).toBe('BUTTON')
      expect(accessibleName(button.element)).toBe(name)

      const srOnly = button.find('.sr-only')
      expect(srOnly.exists()).toBe(true)
      expect(srOnly.text()).toBe(name)

      const icon = button.find('svg')
      expect(icon.exists()).toBe(true)
      expect(icon.attributes('aria-hidden')).toBe('true')
    }
  })

  it('defaults showInteractive to false and keeps the library position default', () => {
    const wrapper = mount(FlowControls)

    expect(wrapper.find('.vue-flow__controls-interactive').exists()).toBe(false)
    const panel = wrapper.find('.vue-flow__panel')
    expect(panel.classes()).toContain('bottom')
    expect(panel.classes()).toContain('left')
  })

  it('passes showInteractive through to the underlying Controls', () => {
    const wrapper = mount(FlowControls, { props: { showInteractive: true } })

    expect(wrapper.find('.vue-flow__controls-interactive').exists()).toBe(true)
    expect(wrapper.findAll('button')).toHaveLength(NAMED_BUTTONS.length + 1)
  })

  it('passes position through (the lifecycle renderer renders bottom-right)', () => {
    const wrapper = mount(FlowControls, { props: { position: 'bottom-right' } })

    const panel = wrapper.find('.vue-flow__panel')
    expect(panel.classes()).toContain('bottom')
    expect(panel.classes()).toContain('right')
  })
})
