import { computed } from 'vue'
import { decodeJwtPayload } from '../lib/jwt'
import { getAccessToken } from '../lib/api/client'

export interface JwtPayload {
  sub?: string
  org_id?: string
  org_role?: string
  is_system_admin?: boolean
  permissions?: unknown
}

export function useCurrentUser() {
  const jwtPayload = computed<JwtPayload | null>(() =>
    decodeJwtPayload(getAccessToken()) as JwtPayload | null,
  )

  const userId = computed(() => jwtPayload.value?.sub ?? null)
  const orgId = computed(() => jwtPayload.value?.org_id ?? null)
  const orgRole = computed(() => jwtPayload.value?.org_role ?? null)
  const isSystemAdmin = computed(() => jwtPayload.value?.is_system_admin === true)
  const isOperator = computed(() => orgRole.value === 'operator' || orgRole.value === 'admin')
  const permissions = computed(() => jwtPayload.value?.permissions ?? null)

  return {
    jwtPayload,
    userId,
    orgId,
    orgRole,
    isSystemAdmin,
    isOperator,
    permissions,
  }
}
