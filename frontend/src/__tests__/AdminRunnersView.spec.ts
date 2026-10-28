import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AdminRunnersView from '../views/AdminRunnersView.vue'

const getMock = vi.hoisted(() => vi.fn())

vi.mock('../lib/api/client', () => ({
  api: { GET: getMock },
}))

vi.mock('../components/FeatureGate.vue', () => ({
  default: { template: '<div data-testid="feature-gate"><slot /></div>' },
}))

vi.mock('../components/shared/PageHeader.vue', () => ({
  default: {
    name: 'PageHeader',
    props: ['title', 'subtitle', 'tabs'],
    template: '<header><h1>{{ title }}</h1><nav><a v-for="tab in tabs" :key="tab.to" :href="tab.to">{{ tab.label }}</a></nav></header>',
  },
}))

vi.mock('../components/runners/RunnerStatusStrip.vue', () => ({
  default: {
    name: 'RunnerStatusStrip',
    props: ['status', 'errored'],
    template: '<div data-testid="runner-status-strip">{{ errored ? "failed" : status?.aggregate_state ?? "loading" }}</div>',
  },
}))

vi.mock('../components/shared/ErrorAlert.vue', () => ({
  default: {
    name: 'ErrorAlert',
    props: ['message'],
    template: '<div data-testid="runner-status-error">{{ message }}</div>',
  },
}))

vi.mock('vue-router', () => ({
  RouterView: {
    template: '<div data-testid="router-view"><slot :Component="null" /></div>',
  },
}))

const status = {
  aggregate_state: 'healthy',
  profiles: [{ provider_type: 'runner_docker' }],
  machines: [],
}

describe('AdminRunnersView', () => {
  beforeEach(() => {
    getMock.mockReset()
    getMock.mockResolvedValue({ data: status, error: undefined })
  })

  it('loads runner status and renders both runner navigation tabs', async () => {
    const wrapper = mount(AdminRunnersView, {
      global: { stubs: { RouterView: { template: '<div><slot :Component="null" /></div>' } } },
    })
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/runners/status')
    expect(wrapper.find('a[href="/admin/runners/profiles"]').exists()).toBe(true)
    expect(wrapper.find('a[href="/admin/runners/concurrency"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="runner-status-strip"]').text()).toBe('healthy')
  })

  it('renders the status error and failed strip when the API request fails', async () => {
    getMock.mockResolvedValue({ data: undefined, error: { detail: 'runner service unavailable' } })

    const wrapper = mount(AdminRunnersView, {
      global: { stubs: { RouterView: { template: '<div><slot :Component="null" /></div>' } } },
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="runner-status-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="runner-status-error"]').text()).toContain('runner status unavailable')
    expect(wrapper.find('[data-testid="runner-status-strip"]').text()).toBe('failed')
  })
})
