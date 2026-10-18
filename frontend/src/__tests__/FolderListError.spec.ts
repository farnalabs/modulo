import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import FolderListError from '../components/pipelines/FolderListError.vue'

// The shared folder-list failure surface. The four call-site specs pin the
// rendered behaviour; this spec pins the component's OWN contract, which the
// refactor depends on: attrs fall through to the single alert root (no
// wrapper), the breakpoint gate is opt-in, retry is wired, and the surface is
// not dismissible by default.
//
// Accessor note: FolderListError's template opens with a contract comment, so
// Vue Test Utils resolves `wrapper.element` to the mount container rather than
// the alert root. The root is therefore located by its role/testid - which is
// also what makes the "no wrapper" assertion meaningful: the caller's testid
// must sit on the role="alert" element itself.

function mountError(attrs: Record<string, unknown> = {}, gated = false) {
  return mount(FolderListError, {
    props: { message: 'boom', onRetry: vi.fn(), gated },
    attrs,
  })
}

describe('FolderListError', () => {
  it('renders the caller testid on the alert root, with no wrapper element', () => {
    const wrapper = mountError({ 'data-testid': 'probe-folder-error' })
    const root = wrapper.find('[data-testid="probe-folder-error"]')
    expect(root.exists()).toBe(true)
    // Exactly one node carries the testid - a wrapper would duplicate the surface.
    expect(wrapper.findAll('[data-testid="probe-folder-error"]').length).toBe(1)
    // The testid sits on the role="alert" element itself, not a wrapper around it.
    expect(root.attributes('role')).toBe('alert')
  })

  it('merges the parent class onto the alert root', () => {
    const wrapper = mountError({ class: 'mb-4' })
    expect(wrapper.find('[role="alert"]').classes()).toContain('mb-4')
  })

  it('adds md:hidden to the alert root when gated is true', () => {
    const wrapper = mountError({}, true)
    expect(wrapper.find('[role="alert"]').classes()).toContain('md:hidden')
  })

  it('omits md:hidden from the alert root when gated is not supplied', () => {
    const wrapper = mountError()
    expect(wrapper.find('[role="alert"]').classes()).not.toContain('md:hidden')
  })

  it('invokes onRetry exactly once when the retry button is clicked', async () => {
    const onRetry = vi.fn()
    const wrapper = mount(FolderListError, {
      props: { message: 'boom', onRetry },
    })
    const retry = wrapper.find('[data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)
    await retry.trigger('click')
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('renders no dismiss affordance by default', () => {
    const wrapper = mountError()
    expect(wrapper.find('[data-testid="error-alert-dismiss"]').exists()).toBe(false)
  })
})
