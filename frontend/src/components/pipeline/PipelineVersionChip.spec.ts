import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import PipelineVersionChip from './PipelineVersionChip.vue'

// FAR-1812: the version chip is an interactive disclosure — hover/focus opens
// the metadata popover, Escape/pointer-leave/focus-leave closes it. These
// paths are exercised here at the component boundary (the editor suite covers
// the chip's integration, not its own keyboard/pointer contract).
function makeSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    snapshot_version: 4,
    created_at: '2026-08-01T12:00:00Z',
    created_kind: 'edit',
    created_by: 'acct-0123456789abcdef',
    tag: null,
    notes: null,
    ...overrides,
  }
}

function mountChip(props: Record<string, unknown> = {}) {
  return mount(PipelineVersionChip, {
    props: { snapshot: makeSnapshot(), mode: 'latest', ...props } as never,
  })
}

describe('PipelineVersionChip', () => {
  it('renders the raw created_kind when it is outside the closed vocabulary', async () => {
    const wrapper = mountChip({ snapshot: makeSnapshot({ created_kind: 'imported' }) })
    await wrapper.find('[data-testid="pipeline-editor-version-chip"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-kind"]').text()).toBe('imported')
    wrapper.unmount()
  })

  it('falls back to the no-value label for an empty created_kind and unknown author', async () => {
    const wrapper = mountChip({
      snapshot: makeSnapshot({ created_kind: '', created_by: null }),
    })
    await wrapper.find('[data-testid="pipeline-editor-version-chip"]').trigger('click')
    await nextTick()

    const popover = wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]')
    // created_kind '' → the `|| t(no_value)` arm; created_by null → the
    // unknown-author arm. Both render the em-dash placeholder.
    expect(popover.text()).toContain('\u2014')
    expect(popover.find('[data-testid="pipeline-editor-version-chip-author"]').text()).toContain('\u2014')
    expect(popover.find('[data-testid="pipeline-editor-version-chip-kind"]').text()).toContain('\u2014')
    wrapper.unmount()
  })

  it('opens on click and closes on mouseleave of the wrapper', async () => {
    const wrapper = mountChip()
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(false)

    await wrapper.find('[data-testid="pipeline-editor-version-chip"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(true)

    await wrapper.find('.relative').trigger('mouseleave')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the popover open while focus moves within the wrapper and closes it on focus leave', async () => {
    const wrapper = mountChip()
    await wrapper.find('[data-testid="pipeline-editor-version-chip"]').trigger('focusin')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(true)

    const inner = wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').element
    await wrapper.find('.relative').trigger('focusout', { relatedTarget: inner })
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(true)

    const outside = document.createElement('button')
    await wrapper.find('.relative').trigger('focusout', { relatedTarget: outside })
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('closes the popover on Escape but ignores other keys, and never swallows form-control keys', async () => {
    const wrapper = mountChip()
    const vm = wrapper.vm as unknown as {
      onKeydown: (e: KeyboardEvent) => void
    }

    await wrapper.find('[data-testid="pipeline-editor-version-chip"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(true)

    // A non-Escape key does not close.
    await wrapper.find('.relative').trigger('keydown', { key: 'a' })
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(true)

    // Escape closes the popover.
    await wrapper.find('[data-testid="pipeline-editor-version-chip"]').trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(false)

    // The INPUT/TEXTAREA guard returns early — the container never handles a
    // form control's keys, so Escape typed into one is not intercepted.
    await wrapper.find('[data-testid="pipeline-editor-version-chip"]').trigger('click')
    await nextTick()
    const guard = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })
    Object.defineProperty(guard, 'target', { value: { tagName: 'INPUT' } })
    vm.onKeydown(guard)
    expect(guard.defaultPrevented).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
