import createClient from 'openapi-fetch'
import type { paths } from './schema'
import { toProblemDetail } from './formatError'
import {
  getAuthHeaders,
  attemptTokenRefresh,
  clearAccessToken,
  redirectToLogin,
} from './auth'

export {
  getAccessToken,
  clearAccessToken,
  setAccessToken,
  setRefreshToken,
  onAuthChange,
  getAuthHeaders,
} from './auth'

// Decides the app's initial auth state at startup (used by App.vue). An
// existing stored token means the user is already authenticated regardless of
// auto-login config; auto-login only runs when no session exists yet. The
// hasAutoLogin arg is deliberately not consulted so a configured auto-login can
// never force a false-negative start for a user who already has a valid token.
export function getInitialAuthState(hasToken: boolean, _hasAutoLogin: boolean): boolean {
  return hasToken
}

export const api = createClient<paths>({
  baseUrl: '',
  headers: getAuthHeaders(),
})

// openapi-fetch doesn't support dynamic headers, so we wrap the methods
// to inject the auth token on every request.
const _origGet = api.GET
const _origPost = api.POST
const _origPut = api.PUT
const _origPatch = api.PATCH
const _origDelete = api.DELETE

function withAuth(fn: (...args: any[]) => any) {
  return async (...args: any[]) => {
    const [url, options] = args
    const headers = { ...getAuthHeaders(), ...options?.headers }
    let resp = await fn(url, { ...options, headers })
    if (resp.response?.status === 401) {
      const refreshed = await attemptTokenRefresh()
      if (refreshed) {
        const newHeaders = { ...getAuthHeaders(), ...options?.headers }
        resp = await fn(url, { ...options, headers: newHeaders })
      }
      if (!refreshed || resp.response?.status === 401) {
        clearAccessToken()
        redirectToLogin()
        return { response: undefined, data: undefined, error: undefined } as any
      }
    }
    if (resp.error && typeof resp.error === 'object') {
      resp.error = toProblemDetail(resp.error) as any
    }
    return resp
  }
}

api.GET = withAuth(_origGet) as any
api.POST = withAuth(_origPost) as any
api.PUT = withAuth(_origPut) as any
api.PATCH = withAuth(_origPatch) as any
api.DELETE = withAuth(_origDelete) as any

export type { paths, components } from './schema'
