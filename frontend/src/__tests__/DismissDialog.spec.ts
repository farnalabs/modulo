import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import DismissDialog from '../components/DismissDialog.vue'

type RectOverride = { top?: number; left?: number; bottom?: number; right?: number; width?: number; height?: number }
function makeTrigger(rect: RectOverride = {}) {
  const full = { top: 100, left: 100, bottom: 140, right: 240, width: 140, height: 40, ...rect }
  return {
    getBoundingClientRect: () => ({
      x: full.left,
      y: full.top,
      top: full.top,
      left: full.left,
      bottom: full.bottom,
      right: full.right,
      width: full.width,
      height: full.height,
      toJSON: () => full,
    }),
  } as unknown as HTMLElement
}

function setViewport(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true, writable: true })
  Object.defineProperty(window, 'innerHeight', { value: height, configurable: true, writable: true })
}

function makeNotification(overrides: Record<string, unknown> = {}) {
  return {
    scope: 'org',
    dismiss_strategy: 'user_only',
    dismissible_at_scope: false,
    ...overrides,
  }
}

describe('DismissDialog', () => {
  beforeEach(() => {
    setViewport(1024, 768)
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('renders nothing when modelValue is false', () => {
    const wrapper = mount(DismissDialog, {
      props: { notification: makeNotification(), modelValue: false },
    })
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(wrapper.html()).not.toContain('dismiss')
  })

  it('opens and focuses the first radio when modelValue becomes true', async () => {
    const wrapper = mount(DismissDialog, {
      props: { notification: makeNotification({ dismissible_at_scope: true }), modelValue: false, triggerRef: makeTrigger() },
    })
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    await nextTick()
    const dialog = document.body.querySelector('[role="dialog"]') as HTMLElement
    expect(dialog).not.toBeNull()
    const firstRadio = dialog.querySelector('input[type="radio"]') as HTMLInputElement
    expect(firstRadio).toBeTruthy()
    expect(document.activeElement).toBe(firstRadio)
  })

  it('resets selectedScope to "self" each time it opens', async () => {
    const wrapper = mount(DismissDialog, {
      props: { notification: makeNotification(), modelValue: false, triggerRef: makeTrigger() },
    })
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    await nextTick()
    const radios = document.body.querySelectorAll('input[type="radio"]') as NodeListOf<HTMLInputElement>
    // "self" is the first radio and is checked by default
    expect((radios[0] as HTMLInputElement).checked).toBe(true)
  })

  it('emits confirm with selected scope and closes when confirm is clicked', async () => {
    const wrapper = mount(DismissDialog, {
      props: { notification: makeNotification({ dismissible_at_scope: true }), modelValue: false, triggerRef: makeTrigger() },
    })
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    await nextTick()
    const confirmBtn = document.body.querySelector('[role="dialog"] button:last-child') as HTMLButtonElement
    await confirmBtn.click()
    await nextTick()
    const confirms = wrapper.emitted('confirm')
    expect(confirms).toBeTruthy()
    expect(confirms?.[0]).toEqual(['self'])
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([false])
  })

  it('emits update:modelValue false when the backdrop is clicked', async () => {
    const wrapper = mount(DismissDialog, {
      props: { notification: makeNotification(), modelValue: false, triggerRef: makeTrigger() },
    })
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    await nextTick()
    const backdrop = document.body.querySelector('.fixed.inset-0.z-50') as HTMLElement
    await backdrop.click()
    await nextTick()
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([false])
  })

  it('emits update:modelValue false on Escape', async () => {
    const wrapper = mount(DismissDialog, {
      props: { notification: makeNotification(), modelValue: false, triggerRef: makeTrigger() },
    })
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    await nextTick()
    const backdrop = document.body.querySelector('.fixed.inset-0.z-50') as HTMLElement
    backdrop.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([false])
  })

  it('hides the scope option when not dismissible at scope', async () => {
    const wrapper = mount(DismissDialog, {
      props: { notification: makeNotification({ dismissible_at_scope: false }), modelValue: false, triggerRef: makeTrigger() },
    })
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    await nextTick()
    const radios = document.body.querySelectorAll('input[type="radio"]') as NodeListOf<HTMLInputElement>
    expect(radios.length).toBe(1)
  })

  describe('positionPanel', () => {
    it('centers the panel when no triggerRef is provided', async () => {
      const wrapper = mount(DismissDialog, {
        props: { notification: makeNotification(), modelValue: false },
      })
      await wrapper.setProps({ modelValue: true })
      await flushPromises()
      await nextTick()
      const dialog = document.body.querySelector('[role="dialog"]') as HTMLElement
      expect(dialog.style.left).toBe('50%')
      expect(dialog.style.top).toBe('50%')
      expect(dialog.style.transform).toBe('translate(-50%, -50%)')
    })

    it('places the panel below the trigger when it fits', async () => {
      setViewport(1024, 768)
      const wrapper = mount(DismissDialog, {
        props: { notification: makeNotification(), modelValue: false, triggerRef: makeTrigger({ top: 100, bottom: 140, left: 200 }) },
      })
      await wrapper.setProps({ modelValue: true })
      await flushPromises()
      await nextTick()
      const dialog = document.body.querySelector('[role="dialog"]') as HTMLElement
      // top = bottom + gap (8) = 148; left = 200
      expect(dialog.style.top).toBe('148px')
      expect(dialog.style.left).toBe('200px')
    })

    it('flips above the trigger when it would overflow the bottom', async () => {
      setViewport(1024, 768)
      const wrapper = mount(DismissDialog, {
        props: { notification: makeNotification(), modelValue: false, triggerRef: makeTrigger({ top: 700, bottom: 740, left: 200 }) },
      })
      await wrapper.setProps({ modelValue: true })
      await flushPromises()
      await nextTick()
      const dialog = document.body.querySelector('[role="dialog"]') as HTMLElement
      // top = trigger.top - panelHeight(260) - gap(8) = 432
      expect(dialog.style.top).toBe('432px')
    })

    it('shifts left when it would overflow the right edge', async () => {
      setViewport(1024, 768)
      const wrapper = mount(DismissDialog, {
        props: { notification: makeNotification(), modelValue: false, triggerRef: makeTrigger({ top: 100, bottom: 140, left: 1000 }) },
      })
      await wrapper.setProps({ modelValue: true })
      await flushPromises()
      await nextTick()
      const dialog = document.body.querySelector('[role="dialog"]') as HTMLElement
      // left = innerWidth(1024) - panelWidth(340) - 16 = 668
      expect(dialog.style.left).toBe('668px')
    })

    it('clamps to the left margin when it would overflow the left edge', async () => {
      setViewport(1024, 768)
      const wrapper = mount(DismissDialog, {
        props: { notification: makeNotification(), modelValue: false, triggerRef: makeTrigger({ top: 100, bottom: 140, left: -50 }) },
      })
      await wrapper.setProps({ modelValue: true })
      await flushPromises()
      await nextTick()
      const dialog = document.body.querySelector('[role="dialog"]') as HTMLElement
      expect(dialog.style.left).toBe('16px')
    })

    it('centers vertically when it would overflow the top after flipping', async () => {
      setViewport(1024, 200)
      const wrapper = mount(DismissDialog, {
        props: { notification: makeNotification(), modelValue: false, triggerRef: makeTrigger({ top: 150, bottom: 190, left: 200 }) },
      })
      await wrapper.setProps({ modelValue: true })
      await flushPromises()
      await nextTick()
      const dialog = document.body.querySelector('[role="dialog"]') as HTMLElement
      // top would be 150 - 260 - 8 = negative -> clamp to (200 - 260)/2 = -30 -> max(16, -30) = 16
      expect(dialog.style.top).toBe('16px')
    })
  })
})
