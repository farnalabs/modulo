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
})
