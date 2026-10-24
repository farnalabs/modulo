import { describe, it, expect, beforeEach, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: { permissions: [] }, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue(null),
}))

import {
  ORG_LICENSE_MANAGE_PERMISSION,
  SYSTEM_CONFIG_MANAGE_PERMISSION,
  useLicenceAccess,
} from '../composables/useLicenceAccess'
import { usePlanStore } from '../stores/planStore'

// The access derivation is a pure function of the caller-scoped permission
// list. These tests pin the two permissions to the two scopes and prove the
// fail-closed default (an empty list grants nothing).
describe('useLicenceAccess', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('pins the permission keys the licence endpoints enforce', () => {
    expect(ORG_LICENSE_MANAGE_PERMISSION).toBe('org.license.manage')
    expect(SYSTEM_CONFIG_MANAGE_PERMISSION).toBe('system.config.manage')
  })

  it('grants org licence management to a holder of org.license.manage', () => {
    const planStore = usePlanStore()
    // Terminal state: the composable's fire-and-forget fetch is a no-op, so
    // the list below is the one the derivation reads.
    planStore.permissionsSource = 'server'
    planStore.userPermissions = ['org.license.manage']

    const { canManageOrgLicence, canManageSystemLicence } = useLicenceAccess()

    expect(canManageOrgLicence.value).toBe(true)
    expect(canManageSystemLicence.value).toBe(false)
  })

  it('grants the system controls only to a holder of system.config.manage', () => {
    const planStore = usePlanStore()
    planStore.permissionsSource = 'server'
    planStore.userPermissions = ['org.license.manage', 'system.config.manage']

    const { canManageOrgLicence, canManageSystemLicence } = useLicenceAccess()

    expect(canManageOrgLicence.value).toBe(true)
    expect(canManageSystemLicence.value).toBe(true)
  })

  it('fails closed on an empty permission list', () => {
    const planStore = usePlanStore()
    planStore.permissionsSource = 'server'
    planStore.userPermissions = []

    const { canManageOrgLicence, canManageSystemLicence } = useLicenceAccess()

    expect(canManageOrgLicence.value).toBe(false)
    expect(canManageSystemLicence.value).toBe(false)
  })
})
