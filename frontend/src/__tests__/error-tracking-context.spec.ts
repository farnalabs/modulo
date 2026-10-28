import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const { getAccessTokenMock, getActivePiniaMock } = vi.hoisted(() => ({
  getAccessTokenMock: vi.fn<() => string | null>(() => 'token-1'),
  getActivePiniaMock: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  getAccessToken: getAccessTokenMock,
}))
vi.mock('pinia', async (importOriginal) => {
  const actual = await importOriginal<typeof import('pinia')>()
  return { ...actual, getActivePinia: getActivePiniaMock }
})

import { gatherContext } from '../lib/error-tracking/context'
import { usePlanStore } from '../stores/planStore'

describe('error-tracking context', () => {
  beforeEach(() => {
    getAccessTokenMock.mockReturnValue('token-1')
    getActivePiniaMock.mockReset()
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    setActivePinia(createPinia())
  })

  it('includes browser details, plan identity, and auth state', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    getActivePiniaMock.mockReturnValue(pinia)
    const plan = usePlanStore()
    plan.$patch({ currentTier: 'team', orgId: 'org-1' })

    const context = gatherContext()

    expect(context.url).toBe(window.location.href)
    expect(context.viewport).toEqual({ width: window.innerWidth, height: window.innerHeight })
    expect(context.userAgent).toBe(navigator.userAgent)
    expect(context.tier).toBe('team')
    expect(context.orgName).toBe('org-1')
    expect(context.hasAuth).toBe(true)
  })

  it('omits unset plan fields and reports an unauthenticated session', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    getActivePiniaMock.mockReturnValue(pinia)
    getAccessTokenMock.mockReturnValue(null)

    const context = gatherContext()

    expect(context.tier).toBe('community')
    expect(context.orgName).toBeUndefined()
    expect(context.hasAuth).toBe(false)
  })

  it('continues when Pinia access throws', () => {
    getActivePiniaMock.mockImplementation(() => {
      throw new Error('pinia unavailable')
    })

    const context = gatherContext()

    expect(context.hasAuth).toBe(true)
    expect(console.warn).toHaveBeenCalledWith(
      '[error-tracking] pinia not initialized during context gather',
      expect.any(Error),
    )
  })
})
