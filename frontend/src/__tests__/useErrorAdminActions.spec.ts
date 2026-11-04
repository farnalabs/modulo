import { describe, it, expect, beforeEach, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: { permissions: [] }, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue(null),
}))

import {
  ERROR_MANAGE_PERMISSION,
  useErrorAdminActions,
} from '../composables/useErrorAdminActions'
import { usePlanStore } from '../stores/planStore'

// The error-record write capability is a pure function of the caller-scoped
// permission list. These tests pin the key the PATCH endpoint enforces and
// prove the fail-closed default (an empty list grants nothing), so the view's
// disabled controls cannot drift from the backend registry.
describe('useErrorAdminActions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('pins the permission key the error PATCH endpoint enforces', () => {
    expect(ERROR_MANAGE_PERMISSION).toBe('errors.manage')
  })

  it('grants write capability to a holder of errors.manage', () => {
    const planStore = usePlanStore()
    // Terminal state: the composable's fire-and-forget fetch is a no-op, so
    // the list below is the one the derivation reads.
    planStore.permissionsSource = 'server'
    planStore.userPermissions = ['errors.resolve', 'errors.manage']

    const { canManageErrors } = useErrorAdminActions()

    expect(canManageErrors.value).toBe(true)
  })

  it('denies write capability to a read-only holder of errors.resolve', () => {
    const planStore = usePlanStore()
    planStore.permissionsSource = 'server'
    planStore.userPermissions = ['errors.resolve']

    const { canManageErrors } = useErrorAdminActions()

    expect(canManageErrors.value).toBe(false)
  })

  it('fails closed on an empty permission list', () => {
    const planStore = usePlanStore()
    planStore.permissionsSource = 'server'
    planStore.userPermissions = []

    const { canManageErrors } = useErrorAdminActions()

    expect(canManageErrors.value).toBe(false)
  })
})
