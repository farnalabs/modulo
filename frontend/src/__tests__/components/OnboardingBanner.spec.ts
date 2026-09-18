import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('token'),
  clearAccessToken: vi.fn(),
  isDemoSession: vi.fn().mockReturnValue(false),
}))

import OnboardingBanner from '../../components/onboarding/OnboardingBanner.vue'
import { useOnboardingStore, type OnboardingAction } from '../../composables/useOnboarding'

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: '/', name: 'dashboard', component: { template: '<div>Dashboard</div>' } }],
})

function makeAction(over: Partial<OnboardingAction>): OnboardingAction {
  return {
    id: 'login',
    title: 'Log in',
    description: 'Sign in',
    order: 1,
    icon: 'user',
    route: '/',
    completed: false,
    skipped: false,
    auto_check: false,
    ...over,
  }
}

describe('OnboardingBanner', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the banner when onboarding is active', async () => {
    const store = useOnboardingStore()
    store.ready = true
    store.isFirstRun = true
    store.dismissed = false
    store.progressPct = 20
    store.actions = [
      makeAction({ id: 'a1', completed: false, skipped: false }),
      makeAction({ id: 'a2', completed: true, skipped: false }),
      makeAction({ id: 'a3', completed: false, skipped: true }),
    ]
    const wrapper = mount(OnboardingBanner, {
      global: { plugins: [router] },
    })
    await nextTick()
    await flushPromises()
    const trigger = wrapper.find('[data-testid="onboarding-banner-trigger"]')
    expect(trigger.exists()).toBe(true)
    // Expand the banner so the action checklist (and its :class bindings)
    // renders and is covered.
    await trigger.trigger('click')
    await nextTick()
    await flushPromises()
    expect(wrapper.find('[data-testid="onboarding-banner-checklist"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="onboarding-action-a1"]').exists()).toBe(true)
  })
})
