import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({ api: { GET: vi.fn(), POST: vi.fn() } }))
import OnboardingBanner from '../components/onboarding/OnboardingBanner.vue'
import { useOnboardingStore, type OnboardingAction } from '../composables/useOnboarding'

const router = createRouter({ history: createWebHistory(), routes: [{ path: '/', component: { template: '<div />' } }, { path: '/pipelines', component: { template: '<div />' } }] })
function action(overrides: Partial<OnboardingAction> = {}): OnboardingAction {
  return { id: 'one', title: 'Create', description: 'Build', order: 1, icon: 'box', route: '/pipelines', completed: false, skipped: false, auto_check: false, ...overrides }
}
function mountBanner() { return mount(OnboardingBanner, { global: { plugins: [router] } }) }

describe('OnboardingBanner coverage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const store = useOnboardingStore()
    store.ready = true; store.isFirstRun = true; store.dismissed = false; store.progressPct = 20
    store.actions = [action(), action({ id: 'done', completed: true }), action({ id: 'skipped', skipped: true, route: null })]
    store.error = null
  })
  afterEach(() => vi.restoreAllMocks())

  it('renders all action states and toggles the checklist', async () => {
    const wrapper = mountBanner()
    await nextTick()
    await wrapper.find('[data-testid="onboarding-banner-trigger"]').trigger('click')
    expect(wrapper.find('[data-testid="onboarding-banner-checklist"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="onboarding-action-one"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="onboarding-action-done"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="onboarding-action-skipped"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-testid="onboarding-skip-action"]')).toHaveLength(1)
  })

  it('routes active actions and does not route completed or skipped actions', async () => {
    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    const wrapper = mountBanner()
    await wrapper.find('[data-testid="onboarding-banner-trigger"]').trigger('click')
    await wrapper.find('[data-testid="onboarding-action-one"]').trigger('click')
    expect(push).toHaveBeenCalledWith('/pipelines')
    await wrapper.find('[data-testid="onboarding-banner-trigger"]').trigger('click')
    await wrapper.find('[data-testid="onboarding-action-done"]').trigger('click')
    await wrapper.find('[data-testid="onboarding-action-skipped"]').trigger('click')
    expect(push).toHaveBeenCalledOnce()
  })

  it('runs skip, dismiss, seed, and initial fetch handlers', async () => {
    const store = useOnboardingStore(); store.ready = false
    store.skipAction = vi.fn().mockResolvedValue(undefined); store.dismiss = vi.fn().mockResolvedValue(undefined)
    store.seedExamples = vi.fn().mockResolvedValue(undefined); store.fetchStatus = vi.fn().mockResolvedValue(undefined)
    const wrapper = mountBanner(); await flushPromises(); expect(store.fetchStatus).toHaveBeenCalledOnce()
    store.ready = true; await nextTick()
    await wrapper.find('[data-testid="onboarding-banner-trigger"]').trigger('click')
    await wrapper.find('[data-testid="onboarding-skip-action"]').trigger('click')
    await wrapper.find('[data-testid="onboarding-dismiss"]').trigger('click')
    await wrapper.find('[data-testid="onboarding-banner-trigger"]').trigger('click')
    await wrapper.find('[data-testid="onboarding-seed-examples"]').trigger('click')
    expect(store.skipAction).toHaveBeenCalledWith('one'); expect(store.dismiss).toHaveBeenCalledOnce(); expect(store.seedExamples).toHaveBeenCalledOnce()
  })

  it('shows errors and removes the inactive banner', async () => {
    const store = useOnboardingStore(); store.error = 'Status failed'
    const wrapper = mountBanner(); await wrapper.find('[data-testid="onboarding-banner-trigger"]').trigger('click')
    expect(wrapper.text()).toContain('Status failed'); store.progressPct = 100; await nextTick()
    expect(wrapper.find('[data-testid="onboarding-banner-trigger"]').exists()).toBe(false)
  })
})
