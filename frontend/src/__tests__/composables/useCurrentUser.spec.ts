import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useCurrentUser } from '../../composables/useCurrentUser'
import { getAccessToken } from '../../lib/api/client'

vi.mock('../../lib/api/client', () => ({
  getAccessToken: vi.fn(),
}))

const accessToken = vi.mocked(getAccessToken)

function tokenFor(payload: Record<string, unknown>): string {
  const encoded = btoa(JSON.stringify(payload)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
  return `header.${encoded}.signature`
}

describe('useCurrentUser', () => {
  beforeEach(() => {
    accessToken.mockReturnValue(null)
  })

  it('exposes identity, organisation, role, admin, operator and permissions claims', () => {
    accessToken.mockReturnValue(tokenFor({
      sub: 'user-1',
      org_id: 'org-1',
      org_role: 'operator',
      is_system_admin: true,
      permissions: ['pipeline.read'],
    }))

    const currentUser = useCurrentUser()

    expect(currentUser.jwtPayload.value).toMatchObject({ sub: 'user-1', org_id: 'org-1' })
    expect(currentUser.userId.value).toBe('user-1')
    expect(currentUser.orgId.value).toBe('org-1')
    expect(currentUser.orgRole.value).toBe('operator')
    expect(currentUser.isSystemAdmin.value).toBe(true)
    expect(currentUser.isOperator.value).toBe(true)
    expect(currentUser.permissions.value).toEqual(['pipeline.read'])
  })

  it('recognises admins as operators and rejects malformed or absent tokens safely', () => {
    accessToken.mockReturnValue(tokenFor({ org_role: 'admin', is_system_admin: false }))
    const admin = useCurrentUser()
    expect(admin.isOperator.value).toBe(true)
    expect(admin.isSystemAdmin.value).toBe(false)

    accessToken.mockReturnValue('not-a-jwt')
    const malformed = useCurrentUser()
    expect(malformed.jwtPayload.value).toBeNull()
    expect(malformed.userId.value).toBeNull()
    expect(malformed.orgId.value).toBeNull()
    expect(malformed.orgRole.value).toBeNull()
    expect(malformed.isOperator.value).toBe(false)
    expect(malformed.permissions.value).toBeNull()
  })
})
