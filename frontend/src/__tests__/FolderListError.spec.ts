import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

import FolderListError from '../components/pipelines/FolderListError.vue'

// The shared folder-list failure surface. The four call-site specs pin the
// rendered behaviour; this spec pins the component's OWN contract, which the
// refactor depends on: attrs are forwarded to the single alert root (no
// wrapper), the breakpoint gate is opt-in, retry is wired, and the surface is
// not dismissible by default.
//
// Accessor note: the component has a true single element root - the contract
// comment lives in the script, not the template - so `wrapper.element` IS the
// alert root, and direct-root assertions on it are possible.

function mountError(attrs: Record<string, unknown> = {}, gated = false) {
  return mount(FolderListError, {
    props: { message: 'boom', onRetry: vi.fn(), gated },
    attrs,
  })
}

// Every mount runs the component's dev-only unsupported-attr warning. Spy on
// it file-wide so the existing dismissal tests stay quiet and the warning is
// observable in the tests that assert on it.
let warnSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  warnSpy.mockRestore()
})

// Filter the global console.warn spy down to this component's own message:
// unrelated library warnings (e.g. PrimeVue's license notice) must neither
// satisfy nor mask these assertions. Kept strict for OUR message: length 1
// means exactly one FolderListError warning, length 0 means none.
const folderListWarnings = () =>
  warnSpy.mock.calls.filter((call: unknown[]) => String(call[0]).includes('FolderListError:'))

describe('FolderListError', () => {
  it('renders the caller testid on the alert root, with no wrapper element', () => {
    const wrapper = mountError({ 'data-testid': 'probe-folder-error', class: 'mb-4' })
    const root = wrapper.find('[data-testid="probe-folder-error"]')
    expect(root.exists()).toBe(true)
    // Exactly one node carries the testid - a wrapper would duplicate the surface.
    expect(wrapper.findAll('[data-testid="probe-folder-error"]').length).toBe(1)
    // The testid sits on the role="alert" element itself, not a wrapper around it.
    expect(root.attributes('role')).toBe('alert')
    // True single element root: `wrapper.element` IS the alert root.
    expect(wrapper.element.getAttribute('data-testid')).toBe('probe-folder-error')
    expect(wrapper.element.getAttribute('role')).toBe('alert')
    expect(wrapper.element.classList.contains('mb-4')).toBe(true)
  })

  it('merges the parent class onto the alert root alongside the gated class', () => {
    const wrapper = mountError({ class: 'mb-4' }, true)
    const root = wrapper.find('[role="alert"]')
    expect(root.classes()).toContain('mb-4')
    // The forwarded parent class and the component's own `md:hidden` must
    // coexist on the same root - neither may clobber the other.
    expect(root.classes()).toContain('md:hidden')
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

  it('cannot be made dismissible from a call site', () => {
    // Pins the structural refusal: before automatic fallthrough was disabled,
    // ErrorAlert declared onDismiss/dismissLabel, so these attrs fell through
    // and rendered a dismiss button.
    const wrapper = mountError({ 'on-dismiss': vi.fn(), 'dismiss-label': 'Close' })
    expect(wrapper.find('[data-testid="error-alert-dismiss"]').exists()).toBe(false)
  })

  it('refuses every dismiss attr spelling, kebab and camel, in one mount', () => {
    // Vue camelises prop lookup, so a caller may write on-dismiss/dismiss-label/
    // dismiss-icon as kebab attrs or onDismiss/dismissLabel/dismissIcon as camel
    // attrs; all six spellings are passed here at once and all must be refused.
    const wrapper = mountError({
      'on-dismiss': vi.fn(),
      'dismiss-label': 'Close',
      'dismiss-icon': true,
      onDismiss: vi.fn(),
      dismissLabel: 'Close',
      dismissIcon: true,
    })
    expect(wrapper.find('[data-testid="error-alert-dismiss"]').exists()).toBe(false)
  })

  it('ignores a retryable attr so the retry affordance always renders', () => {
    // retry is this surface's only recovery affordance, so a call site passing
    // retryable=false must not be able to remove the retry button. Before the
    // refusal the attr fell through and ErrorAlert rendered no button.
    const wrapper = mountError({ retryable: false })
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
  })

  it('warns in dev when an unsupported attr is dropped', () => {
    mountError({ retryable: false })
    const warnings = folderListWarnings()
    expect(warnings).toHaveLength(1)
    // The message must name the attr so the call site can find what it passed.
    expect(warnings[0][0]).toContain('retryable')
  })

  it('does not warn when no unsupported attr is passed', () => {
    mountError({ 'data-testid': 'probe-folder-error', class: 'mb-4' })
    expect(folderListWarnings()).toHaveLength(0)
  })
})
