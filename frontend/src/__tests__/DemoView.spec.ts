import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import DemoView from '../views/DemoView.vue'

const replaceMock = vi.hoisted(() => vi.fn())
const resolveDemoEntryMock = vi.hoisted(() => vi.fn())

vi.mock('vue-router', () => ({
  useRouter: () => ({ replace: replaceMock }),
}))

vi.mock('../lib/api/demo', () => ({
  resolveDemoEntry: resolveDemoEntryMock,
}))

describe('DemoView', () => {
  beforeEach(() => {
    replaceMock.mockReset()
    resolveDemoEntryMock.mockReset()
  })

  it('renders the hand-off state and redirects to the dashboard after a successful entry resolution', async () => {
    resolveDemoEntryMock.mockResolvedValue('dashboard')

    const wrapper = mount(DemoView)
    expect(wrapper.find('[data-testid="demo-view"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Preparing the demo...')

    await flushPromises()

    expect(resolveDemoEntryMock).toHaveBeenCalledOnce()
    expect(replaceMock).toHaveBeenCalledWith({ name: 'dashboard' })
  })

  it('uses the plain login route when the demo hand-off cannot establish a session', async () => {
    resolveDemoEntryMock.mockResolvedValue('login')

    mount(DemoView)
    await flushPromises()

    expect(replaceMock).toHaveBeenCalledWith({ name: 'login' })
  })
})
