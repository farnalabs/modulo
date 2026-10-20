/**
 * FAR-2020: the nav visibility context resolves `required_permissions` gates
 * from the caller-scoped `GET /api/v1/auth/permissions` read.
 *
 * The defect this pins: the context used to populate `userPermissions` ONLY
 * from a JWT `permissions` claim, which the backend never mints — so the list
 * was always empty and every permission-gated nav item was invisible to every
 * user. These tests prove the list now comes from the endpoint, that the JWT
 * claim remains an override when present, and that a failed read degrades to
 * the (correct) empty-list denial rather than throwing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { isNavItemVisible } from '../config/navigation'
import type { NavItem } from '../config/navigation'
import { useNavVisibilityContext } from '../composables/useNavVisibilityContext'
import { usePlanStore } from '../stores/planStore'

const apiGet = vi.hoisted(() => vi.fn())

/** The decoded JWT payload the composable sees; mutated per test. */
const jwtClaims = vi.hoisted(() => ({ value: {} as Record<string, unknown> }))

vi.mock('../lib/api/client', () => ({
  api: { GET: apiGet, PUT: vi.fn(), DELETE: vi.fn(), POST: vi.fn(), PATCH: vi.fn() },
  getAccessToken: () => 'header.payload.signature',
  isDemoSession: () => false,
}))

vi.mock('../lib/jwt', () => ({
  decodeJwtPayload: () => jwtClaims.value,
}))

/** A nav item gated ONLY by a permission — the shape `/analytics` uses. */
function permissionGatedItem(permissions: string[] = ['analytics.query']): NavItem {
  return {
    to: '/analytics',
    icon: 'BarChart',
    label: 'Analytics',
    labelKey: 'components.SidebarNav.item_analytics',
    requiredPermissions: permissions,
  }
}

describe('useNavVisibilityContext permissions (FAR-2020)', () => {
  beforeEach(() => {
    apiGet.mockReset()
    jwtClaims.value = {}
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('shows a required_permissions item when the endpoint grants the permission', async () => {
    apiGet.mockResolvedValue({
      data: { permissions: ['analytics.query', 'pipeline.list'] },
      error: null,
    })
    const ctx = useNavVisibilityContext()

    await usePlanStore().fetchPermissions()

    expect(ctx.value.userPermissions).toEqual(['analytics.query', 'pipeline.list'])
    expect(isNavItemVisible(permissionGatedItem(), ctx.value)).toBe(true)
  })

  it('hides the item when the resolved permission list is empty', async () => {
    apiGet.mockResolvedValue({ data: { permissions: [] }, error: null })
    const ctx = useNavVisibilityContext()

    await usePlanStore().fetchPermissions()

    expect(ctx.value.userPermissions).toEqual([])
    expect(isNavItemVisible(permissionGatedItem(), ctx.value)).toBe(false)
  })

  it('populates the context from the endpoint when the JWT carries no permissions claim', async () => {
    // The regression: the old context read the JWT claims ONLY, so a token
    // without a (never-minted) `permissions` claim yielded an empty list. The
    // assertion below fails for that implementation.
    jwtClaims.value = { sub: 'user@example.com', org_role: 'viewer' }
    apiGet.mockResolvedValue({
      data: { permissions: ['analytics.query'] },
      error: null,
    })
    const ctx = useNavVisibilityContext()

    await usePlanStore().fetchPermissions()

    expect(apiGet).toHaveBeenCalledWith('/api/v1/auth/permissions')
    expect(ctx.value.userPermissions).toEqual(['analytics.query'])
    expect(isNavItemVisible(permissionGatedItem(), ctx.value)).toBe(true)
  })

  it('prefers a permissions claim when the token carries one', () => {
    jwtClaims.value = { sub: 'user@example.com', permissions: ['analytics.query'] }
    apiGet.mockResolvedValue({ data: { permissions: [] }, error: null })

    const ctx = useNavVisibilityContext()

    expect(ctx.value.userPermissions).toEqual(['analytics.query'])
  })

  it('fails closed and does not throw when the permissions read fails', async () => {
    apiGet.mockResolvedValue({ data: undefined, error: { detail: 'unavailable' } })
    const ctx = useNavVisibilityContext()

    await usePlanStore().fetchPermissions()

    expect(ctx.value.userPermissions).toEqual([])
    expect(isNavItemVisible(permissionGatedItem(), ctx.value)).toBe(false)
  })

  it('requests the caller-scoped endpoint only once per session', async () => {
    apiGet.mockResolvedValue({ data: { permissions: ['analytics.query'] }, error: null })

    useNavVisibilityContext()
    useNavVisibilityContext()
    await usePlanStore().fetchPermissions()
    useNavVisibilityContext()
    await usePlanStore().fetchPermissions()

    expect(apiGet).toHaveBeenCalledTimes(1)
  })
})
