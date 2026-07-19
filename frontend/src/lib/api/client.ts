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
