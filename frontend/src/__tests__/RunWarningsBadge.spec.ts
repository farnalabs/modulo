import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
}))

import RunWarningsBadge from '../components/shared/RunWarningsBadge.vue'

describe('RunWarningsBadge', () => {
  beforeEach(() => {
    push.mockClear()
  })

  function mountBadge(count: number) {
    return mount(RunWarningsBadge, {
      props: { runId: 'run-1', count },
      global: {
        directives: { tooltip: {} },
        mocks: {
          $t: (key: string) => (key === 'components.RunWarningsBadge.n_warnings' ? `${count} warnings` : '1 warning'),
        },
      },
    })
  }

  it('renders a dash when there are no warnings', () => {
    const wrapper = mountBadge(0)
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.text()).toBe('—')
  })

  it('renders the badge with count and navigates to the run warnings anchor on click', async () => {
    const wrapper = mountBadge(2)
    const button = wrapper.find('[data-testid="runs-list-warnings-run-1"]')
    expect(button.exists()).toBe(true)
    expect(button.text()).toContain('2')

    await button.trigger('click')
    await nextTick()
    expect(push).toHaveBeenCalledWith({ path: '/runs/run-1', query: { warn: '1' } })
  })

  it('does not stopPropagation on keydown so Cmd/Ctrl+K can reach the CommandPalette', async () => {
    const wrapper = mountBadge(1)
    const button = wrapper.find('[data-testid="runs-list-warnings-run-1"]')

    // A keydown listener on the parent must still receive the event — the badge
    // dropped the swallow-all `@keydown.stop` that previously broke keyboard
    // shortcuts (e.g. CommandPalette's document-level Cmd/Ctrl+K handler).
    const parent = wrapper.element.parentElement
    let parentSawKeydown = false
    parent?.addEventListener('keydown', () => {
      parentSawKeydown = true
    })

    await button.trigger('keydown', { key: 'k', ctrlKey: true })
    expect(parentSawKeydown).toBe(true)
  })

  it('remains keyboard-operable: click still navigates to the warnings anchor', async () => {
    const wrapper = mountBadge(1)
    const button = wrapper.find('[data-testid="runs-list-warnings-run-1"]')

    await button.trigger('click')
    await nextTick()
    expect(push).toHaveBeenCalledWith({ path: '/runs/run-1', query: { warn: '1' } })
  })
})
