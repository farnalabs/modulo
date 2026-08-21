import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useOnboardingStore, type OnboardingAction } from '../composables/useOnboarding'

const mockActions: OnboardingAction[] = [
  { id: 'login', title: 'Log in', description: 'First login', order: 1, icon: 'log-in', route: '/login', completed: false, skipped: false, auto_check: false },
  { id: 'create_pipeline', title: 'Create pipeline', description: 'Build a pipeline', order: 2, icon: 'workflow', route: '/pipelines', completed: false, skipped: false, auto_check: false },
]

const mockStatus = {
  actions: mockActions,
  progress_pct: 0,
  is_first_run: true,
  dismissed: false,
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}))

describe('useOnboarding', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({
      data: { ...mockStatus, actions: mockActions.map(a => ({ ...a })) },
      error: null,
    })
    ;(api.POST as any).mockResolvedValue({ data: {}, error: null })
  })

  it('starts with default state', () => {
    const store = useOnboardingStore()
    expect(store.actions).toEqual([])
    expect(store.progressPct).toBe(0)
    expect(store.isFirstRun).toBe(true)
    expect(store.dismissed).toBe(false)
    expect(store.loading).toBe(false)
    expect(store.error).toBeNull()
    expect(store.ready).toBe(false)
  })

  it('fetchStatus populates actions and derived state', async () => {
    const store = useOnboardingStore()
    await store.fetchStatus()

    expect(store.actions.length).toBe(2)
    expect(store.progressPct).toBe(0)
    expect(store.isFirstRun).toBe(true)
    expect(store.dismissed).toBe(false)
    expect(store.loading).toBe(false)
    expect(store.ready).toBe(true)
    expect(store.error).toBeNull()
  })

  it('auto-completes the login action when it is incomplete', async () => {
    const store = useOnboardingStore()
    await store.fetchStatus()

    const login = store.actions.find(a => a.id === 'login')!
    expect(login.completed).toBe(true)
  })

  it('fetchStatus surfaces the API error on failure', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: null, error: { message: 'Not found' } })

    const store = useOnboardingStore()
    await store.fetchStatus()

    expect(store.error).toBe('Not found')
    expect(store.ready).toBe(true)
    expect(store.loading).toBe(false)
  })

  it('fetchStatus falls back to a generic error when data is missing', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: null, error: null })

    const store = useOnboardingStore()
    await store.fetchStatus()

    expect(store.error).toBe('No response from server')
  })

  it('fetchStatus catches exceptions from the API call', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockRejectedValue(new Error('Network failure'))

    const store = useOnboardingStore()
    await store.fetchStatus()

    expect(store.error).toBe('Network failure')
    expect(store.loading).toBe(false)
    expect(store.ready).toBe(true)
  })

  it('completeAction posts to the action endpoint and refetches status', async () => {
    const { api } = await import('../lib/api/client')
    const store = useOnboardingStore()
    await store.fetchStatus()

    await store.completeAction('create_pipeline')

    expect(api.POST).toHaveBeenCalledWith('/api/v1/onboarding/actions/{action_id}/complete', {
      params: { path: { action_id: 'create_pipeline' } },
    })
    expect(api.GET).toHaveBeenCalledTimes(2)
  })

  it('completeAction does not refetch when the POST fails', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({ data: null, error: { message: 'Already done' } })

    const store = useOnboardingStore()
    await store.fetchStatus()

    await store.completeAction('create_pipeline')

    expect(store.error).toBe('Already done')
    expect(api.GET).toHaveBeenCalledTimes(1)
  })

  it('skipAction posts to the skip endpoint and refetches status', async () => {
    const { api } = await import('../lib/api/client')
    const store = useOnboardingStore()
    await store.fetchStatus()

    await store.skipAction('create_pipeline')

    expect(api.POST).toHaveBeenCalledWith('/api/v1/onboarding/actions/{action_id}/skip', {
      params: { path: { action_id: 'create_pipeline' } },
    })
    expect(api.GET).toHaveBeenCalledTimes(2)
  })

  it('skipAction does not refetch when the POST fails', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({ data: null, error: { message: 'Already skipped' } })

    const store = useOnboardingStore()
    await store.fetchStatus()

    await store.skipAction('create_pipeline')

    expect(store.error).toBe('Already skipped')
    expect(api.GET).toHaveBeenCalledTimes(1)
  })

  it('dismiss posts and marks the onboarding dismissed', async () => {
    const { api } = await import('../lib/api/client')
    const store = useOnboardingStore()

    await store.dismiss()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/onboarding/dismiss')
    expect(store.dismissed).toBe(true)
  })

  it('dismiss surfaces errors without flipping the dismissed flag', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({ data: null, error: { message: 'Forbidden' } })

    const store = useOnboardingStore()
    await store.dismiss()

    expect(store.error).toBe('Forbidden')
    expect(store.dismissed).toBe(false)
  })

  it('seedExamples returns the seeded data and refetches status', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({ data: { seeded: 3 }, error: null })

    const store = useOnboardingStore()
    const result = await store.seedExamples()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/onboarding/seed-examples')
    expect(result).toEqual({ seeded: 3 })
    expect(api.GET).toHaveBeenCalled()
  })

  it('seedExamples returns null on error and does not refetch', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({ data: null, error: { message: 'Nope' } })

    const store = useOnboardingStore()
    await store.fetchStatus()

    const result = await store.seedExamples()

    expect(result).toBeNull()
    expect(store.error).toBe('Nope')
    expect(api.GET).toHaveBeenCalledTimes(1)
  })

  it('seedExamples catches thrown exceptions and returns null', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockRejectedValue(new Error('boom'))

    const store = useOnboardingStore()
    const result = await store.seedExamples()

    expect(result).toBeNull()
    expect(store.error).toBe('boom')
  })

  it('incompleteActions excludes completed and skipped actions', () => {
    const store = useOnboardingStore()
    store.actions = [
      { ...mockActions[0] },
      { ...mockActions[1], completed: true },
      { ...mockActions[1], skipped: true, id: 'skip-me' },
      { ...mockActions[1], id: 'open' },
    ]

    expect(store.incompleteActions.map(a => a.id)).toEqual(['login', 'open'])
  })

  it('completedCount and totalActions are derived correctly', () => {
    const store = useOnboardingStore()
    store.actions = [
      { ...mockActions[0] },
      { ...mockActions[1], completed: true },
    ]

    expect(store.completedCount).toBe(1)
    expect(store.totalActions).toBe(2)
  })

  it('currentAction returns the first incomplete action by order', () => {
    const store = useOnboardingStore()
    store.actions = [
      { ...mockActions[0], order: 3 },
      { ...mockActions[1], order: 1, id: 'first' },
    ]

    expect(store.currentAction?.id).toBe('first')
  })

  it('currentAction is null when every action is complete or skipped', () => {
    const store = useOnboardingStore()
    store.actions = [
      { ...mockActions[0], completed: true },
      { ...mockActions[1], skipped: true },
    ]

    expect(store.currentAction).toBeNull()
  })

  it('isActive only when ready, first run and not dismissed', () => {
    const store = useOnboardingStore()
    expect(store.isActive).toBe(false)

    store.ready = true
    expect(store.isActive).toBe(true)

    store.isFirstRun = false
    expect(store.isActive).toBe(false)

    store.isFirstRun = true
    store.dismissed = true
    expect(store.isActive).toBe(false)
  })

  it('isActive is false once onboarding progress reaches 100%', () => {
    const store = useOnboardingStore()
    store.ready = true
    expect(store.isActive).toBe(true)

    store.progressPct = 100
    expect(store.isActive).toBe(false)

    store.progressPct = 99.9
    expect(store.isActive).toBe(true)
  })
})
