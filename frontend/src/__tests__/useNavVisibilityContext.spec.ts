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
 *
 * The SECOND defect pinned here is the one that made the first fix inert: the
 * sidebar passes its props down as an override, AppLayout derived that from the
 * same (always-empty) JWT claim, and `[] ?? list` is `[]` — so the sidebar kept
 * discarding the endpoint's list while the command palette (no override) worked.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { isNavItemVisible } from '../config/navigation'
import type { NavItem } from '../config/navigation'
import { useNavVisibilityContext } from '../composables/useNavVisibilityContext'
import { usePlanStore } from '../stores/planStore'
// The REAL auth module: the plan store subscribes to its listener registry, so
// the principal-switch tests drive the same `setAccessToken` / `clearAccessToken`
// choke point the app's login and teardown paths use.
import { clearAccessToken, setAccessToken } from '../lib/api/auth'

const apiGet = vi.hoisted(() => vi.fn())

/** The decoded JWT payload the composable sees; mutated per test. */
const jwtClaims = vi.hoisted(() => ({ value: {} as Record<string, unknown> }))

vi.mock('../lib/api/client', () => ({
  api: { GET: apiGet, PUT: vi.fn(), DELETE: vi.fn(), POST: vi.fn(), PATCH: vi.fn() },
  // Read through the same storage key the real auth module writes, so the store
  // sees the token `setAccessToken` just installed.
  getAccessToken: () => localStorage.getItem('modulo_access_token'),
  isDemoSession: () => false,
}))

vi.mock('../lib/jwt', () => ({
  decodeJwtPayload: () => jwtClaims.value,
  principalSubject: (token: string | null | undefined) => {
    if (!token) return null
    const sub = jwtClaims.value.sub
    return typeof sub === 'string' && sub.length > 0 ? sub : null
  },
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

  // ---------------------------------------------------------------------
  // The override precedence. SidebarNav passes AppLayout's props down as an
  // override object; AppLayout derived `userPermissions` from the JWT claim the
  // backend never mints, so the sidebar ALWAYS passed `[]`. That empty array
  // won over the endpoint list and left `/analytics` hidden in the sidebar
  // while the command palette (no override) showed it.
  // ---------------------------------------------------------------------
  it('treats an EMPTY override as no override, so the sidebar still reads the endpoint list', async () => {
    apiGet.mockResolvedValue({ data: { permissions: ['analytics.query'] }, error: null })
    // Exactly the override SidebarNav builds from AppLayout's props.
    const ctx = useNavVisibilityContext(() => ({
      isSystemAdmin: false,
      userRole: 'viewer',
      userPermissions: [],
    }))

    await usePlanStore().fetchPermissions()

    expect(ctx.value.userPermissions).toEqual(['analytics.query'])
    expect(isNavItemVisible(permissionGatedItem(), ctx.value)).toBe(true)
  })

  it('lets a non-empty override win over the endpoint list', async () => {
    apiGet.mockResolvedValue({ data: { permissions: ['analytics.query'] }, error: null })
    const ctx = useNavVisibilityContext({ userPermissions: ['pipeline.list'] })

    await usePlanStore().fetchPermissions()

    expect(ctx.value.userPermissions).toEqual(['pipeline.list'])
    expect(isNavItemVisible(permissionGatedItem(), ctx.value)).toBe(false)
  })

  it('ignores an empty permissions CLAIM for the same reason', () => {
    // A token carrying `permissions: []` is indistinguishable from one carrying
    // no claim at all — neither may mask the endpoint list.
    jwtClaims.value = { sub: 'user@example.com', permissions: [] }
    const ctx = useNavVisibilityContext()

    expect(ctx.value.userPermissions).toEqual([])
    // Nothing has resolved yet, so the store's fail-closed default applies...
    usePlanStore().userPermissions = ['analytics.query']
    expect(ctx.value.userPermissions).toEqual(['analytics.query'])
  })

  // ---------------------------------------------------------------------
  // FAR-2020 MINOR: the resolved list must not outlive its principal. Logout
  // (or a different account signing in on the same SPA) has to drop it, while
  // a token REFRESH for the same person must keep it.
  // ---------------------------------------------------------------------
  it('drops the resolved list when the principal changes', async () => {
    jwtClaims.value = { sub: 'first@example.com' }
    setAccessToken('first.token')
    apiGet.mockResolvedValue({ data: { permissions: ['analytics.query'] }, error: null })
    const store = usePlanStore()
    const ctx = useNavVisibilityContext()

    await store.fetchPermissions()
    expect(ctx.value.userPermissions).toEqual(['analytics.query'])

    // A second account signs in on the same SPA: different `sub`, new token.
    jwtClaims.value = { sub: 'second@example.com' }
    setAccessToken('second.token')

    expect(store.userPermissions).toEqual([])
    expect(store.permissionsSource).toBe('none')
    expect(isNavItemVisible(permissionGatedItem(), ctx.value)).toBe(false)
  })

  it('drops the resolved list on logout', async () => {
    jwtClaims.value = { sub: 'first@example.com' }
    setAccessToken('first.token')
    apiGet.mockResolvedValue({ data: { permissions: ['analytics.query'] }, error: null })
    const store = usePlanStore()

    await store.fetchPermissions()
    expect(store.userPermissions).toEqual(['analytics.query'])

    clearAccessToken()

    expect(store.userPermissions).toEqual([])
    expect(store.permissionsSource).toBe('none')
  })

  it('keeps the resolved list across a token refresh for the same principal', async () => {
    jwtClaims.value = { sub: 'first@example.com' }
    setAccessToken('first.token')
    apiGet.mockResolvedValue({ data: { permissions: ['analytics.query'] }, error: null })
    const store = usePlanStore()

    await store.fetchPermissions()
    setAccessToken('rotated.token')

    expect(store.userPermissions).toEqual(['analytics.query'])
    // Still 'server': the refresh must not force a second permissions read.
    expect(store.permissionsSource).toBe('server')
  })
})
