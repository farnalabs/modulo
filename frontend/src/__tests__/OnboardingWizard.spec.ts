import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: { id: '1', name: 'Test' }, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    post: vi.fn().mockResolvedValue({ id: '1', name: 'Test' }),
  })),
}))

import OnboardingWizard from '../views/OnboardingWizard.vue'

describe('OnboardingWizard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(OnboardingWizard, {
      global: {
        stubs: { RouterLink: true },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('SDLC Onboarding')
  })

  async function mountAtDoneStep() {
    const wrapper = mount(OnboardingWizard, {
      global: {
        stubs: { RouterLink: true },
      },
    })
    await nextTick()
    const vm = wrapper.vm as any
    vm.wizardState.createdPipelineId = 'pipeline-1'
    vm.wizardState.createdPipelineName = 'Test Pipeline'
    vm.currentStep = 6
    await nextTick()
    return wrapper
  }

  it('guards against a silent empty-input run with a two-click confirmation', async () => {
    const wrapper = await mountAtDoneStep()
    const { api } = await import('../lib/api/client')
    const runButton = wrapper.find('[data-testid="onboarding-wizard-run-pipeline-now"]')

    await runButton.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(true)
    expect(api.POST).not.toHaveBeenCalledWith('/api/v1/runs', expect.anything())

    await runButton.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(false)
    expect(api.POST).toHaveBeenCalledWith('/api/v1/runs', expect.objectContaining({ body: expect.objectContaining({ input_payload: {} }) }))
  })

  it('dismisses the empty-run warning via the Cancel affordance without running', async () => {
    const wrapper = await mountAtDoneStep()
    const { api } = await import('../lib/api/client')
    const runButton = wrapper.find('[data-testid="onboarding-wizard-run-pipeline-now"]')

    await runButton.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(true)

    await wrapper.find('[data-testid="onboarding-wizard-dismiss-empty-warning"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(false)
    expect(api.POST).not.toHaveBeenCalledWith('/api/v1/runs', expect.anything())
  })

  it('resets the empty-run warning when the pipeline description is edited', async () => {
    const wrapper = await mountAtDoneStep()
    const runButton = wrapper.find('[data-testid="onboarding-wizard-run-pipeline-now"]')

    await runButton.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(true)

    const vm = wrapper.vm as any
    vm.wizardState.pipelineDescription = 'edited'
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-wizard-run-empty-warning"]').exists()).toBe(false)
  })
})
