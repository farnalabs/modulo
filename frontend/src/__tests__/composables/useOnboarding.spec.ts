import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { mockGet, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
}))

vi.mock('@/lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: mockPost,
  },
}))

const loginAction = {
  id: 'login',
  title: 'Log in',
  description: 'Sign in to your account',
  order: 0,
  icon: 'log-in',
  route: null,
  completed: false,
  skipped: false,
  auto_check: false,
}

const pipelineAction = {
  id: 'create-pipeline',
  title: 'Create a pipeline',
  description: 'Build your first pipeline',
  order: 1,
  icon: 'workflow',
  route: '/pipelines/new',
  completed: false,
  skipped: false,
  auto_check: true,
}

const statusPayload = {
  actions: [loginAction, pipelineAction],
  progress_pct: 12,
  is_first_run: true,
  dismissed: false,
}

describe('useOnboardingStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: statusPayload, error: undefined })
    mockPost.mockResolvedValue({ data: null, error: undefined })
  })

  it('starts in the default state', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    expect(store.actions).toEqual([])
    expect(store.progressPct).toBe(0)
    expect(store.isFirstRun).toBe(true)
    expect(store.dismissed).toBe(false)
    expect(store.loading).toBe(false)
    expect(store.error).toBeNull()
    expect(store.ready).toBe(false)
    expect(store.isActive).toBe(false)
    expect(store.currentAction).toBeNull()
  })

  it('fetchStatus populates actions, progress, and flags', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.fetchStatus()

    expect(store.actions).toHaveLength(2)
    expect(store.progressPct).toBe(12)
    expect(store.isFirstRun).toBe(true)
    expect(store.dismissed).toBe(false)
    expect(store.error).toBeNull()
    expect(store.loading).toBe(false)
    expect(store.ready).toBe(true)
    expect(mockGet).toHaveBeenCalledWith('/api/v1/onboarding/status')
  })

  it('fetchStatus force-completes the login action', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.fetchStatus()

    const login = store.actions.find(a => a.id === 'login')
    expect(login?.completed).toBe(true)
  })

  it('fetchStatus surfaces the formatted API error', async () => {
    mockGet.mockResolvedValue({ data: undefined, error: { detail: 'not authorised' } })
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.fetchStatus()

    expect(store.error).toBe('not authorised')
    expect(store.actions).toEqual([])
    expect(store.ready).toBe(true)
  })

  it('fetchStatus reports a missing response body', async () => {
    mockGet.mockResolvedValue({ data: undefined, error: undefined })
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.fetchStatus()

    expect(store.error).toBe('No response from server')
  })

  it('fetchStatus catches rejected requests', async () => {
    mockGet.mockRejectedValue(new Error('network down'))
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.fetchStatus()

    expect(store.error).toBe('network down')
  })

  it('toggles loading while fetchStatus is in flight', async () => {
    let resolveGet!: (value: unknown) => void
    mockGet.mockImplementation(() => new Promise((resolve) => { resolveGet = resolve }))
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    const pending = store.fetchStatus()
    expect(store.loading).toBe(true)

    resolveGet({ data: statusPayload, error: undefined })
    await pending
    expect(store.loading).toBe(false)
  })

  it('incompleteActions excludes completed and skipped actions', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()
    await store.fetchStatus()

    const incomplete = store.incompleteActions
    expect(incomplete).toHaveLength(1)
    expect(incomplete[0].id).toBe('create-pipeline')
  })

  it('currentAction returns the first incomplete action by order', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()
    await store.fetchStatus()

    expect(store.currentAction?.id).toBe('create-pipeline')
  })

  it('completedCount and totalActions reflect the fetched actions', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()
    await store.fetchStatus()

    expect(store.completedCount).toBe(1)
    expect(store.totalActions).toBe(2)
  })

  it('isActive requires ready, first run, and not dismissed', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.fetchStatus()
    expect(store.isActive).toBe(true)

    store.isFirstRun = false
    expect(store.isActive).toBe(false)

    store.isFirstRun = true
    store.dismissed = true
    expect(store.isActive).toBe(false)
  })

  it('completeAction posts the action id and refreshes status', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.completeAction('create-pipeline')

    expect(mockPost).toHaveBeenCalledWith('/api/v1/onboarding/actions/{action_id}/complete', {
      params: { path: { action_id: 'create-pipeline' } },
    })
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('completeAction records the API error without refreshing', async () => {
    mockPost.mockResolvedValue({ data: undefined, error: { detail: 'action missing' } })
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.completeAction('create-pipeline')

    expect(store.error).toBe('action missing')
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('completeAction catches rejected requests', async () => {
    mockPost.mockRejectedValue(new Error('boom'))
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.completeAction('create-pipeline')

    expect(store.error).toBe('boom')
  })

  it('skipAction posts the skip endpoint and refreshes status', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.skipAction('create-pipeline')

    expect(mockPost).toHaveBeenCalledWith('/api/v1/onboarding/actions/{action_id}/skip', {
      params: { path: { action_id: 'create-pipeline' } },
    })
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('dismiss posts the dismiss endpoint and flips the flag', async () => {
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.dismiss()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/onboarding/dismiss')
    expect(store.dismissed).toBe(true)
    expect(store.error).toBeNull()
  })

  it('dismiss records an API error without flipping the flag', async () => {
    mockPost.mockResolvedValue({ data: undefined, error: { detail: 'forbidden' } })
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    await store.dismiss()

    expect(store.error).toBe('forbidden')
    expect(store.dismissed).toBe(false)
  })

  it('seedExamples returns the seeded data and refreshes status', async () => {
    mockPost.mockResolvedValue({ data: { count: 3 }, error: undefined })
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    const result = await store.seedExamples()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/onboarding/seed-examples')
    expect(result).toEqual({ count: 3 })
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('seedExamples returns null when seeding fails', async () => {
    mockPost.mockRejectedValue(new Error('seed exploded'))
    const { useOnboardingStore } = await import('../../composables/useOnboarding')
    const store = useOnboardingStore()

    const result = await store.seedExamples()

    expect(result).toBeNull()
    expect(store.error).toBe('seed exploded')
  })
})
