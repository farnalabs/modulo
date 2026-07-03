import createClient from 'openapi-fetch'
import type { paths } from './schema'
import { toProblemDetail } from './formatError'

const TOKEN_KEY = 'modulo_access_token'

let _authListeners: Array<(token: string | null) => void> = []

function notifyListeners(): void {
  const token = localStorage.getItem(TOKEN_KEY)
  for (const fn of _authListeners) {
    fn(token)
  }
}

export function onAuthChange(fn: (token: string | null) => void): () => void {
  _authListeners.push(fn)
  fn(localStorage.getItem(TOKEN_KEY))
  return () => {
    _authListeners = _authListeners.filter((f) => f !== fn)
  }
}

export function setAccessToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
  notifyListeners()
}

export function clearAccessToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  notifyListeners()
}

export function getAccessToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

function getAuthHeaders(): Record<string, string> {
  const token = getAccessToken()
  if (token) {
    return { Authorization: `Bearer ${token}` }
  }
  return {}
}

export const api = createClient<paths>({
  baseUrl: '',
  headers: getAuthHeaders(),
})

// openapi-fetch doesn't support dynamic headers, so we wrap the methods
// to inject the auth token on every request.
const _origGet = api.GET.bind(api)
const _origPost = api.POST.bind(api)
const _origPut = api.PUT.bind(api)
const _origPatch = api.PATCH.bind(api)
const _origDelete = api.DELETE.bind(api)

function withAuth(fn: (...args: any[]) => any) {
  return async (...args: any[]) => {
    const [url, options] = args
    const headers = { ...getAuthHeaders(), ...options?.headers }
    const resp = await fn(url, { ...options, headers })
    if (resp.response?.status === 401) {
      clearAccessToken()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    }
    // Normalize API errors to ProblemDetail so views and ErrorAlert
    // get structured error information they can branch on.
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
