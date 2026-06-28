import createClient from 'openapi-fetch'
import type { paths } from './schema'

const TOKEN_KEY = 'modulo_access_token'

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    return { Authorization: `Bearer ${token}` }
  }
  return {}
}

export function setAccessToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearAccessToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export function getAccessToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
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
  return (...args: any[]) => {
    const [url, options] = args
    const headers = { ...getAuthHeaders(), ...options?.headers }
    return fn(url, { ...options, headers })
  }
}

api.GET = withAuth(_origGet) as any
api.POST = withAuth(_origPost) as any
api.PUT = withAuth(_origPut) as any
api.PATCH = withAuth(_origPatch) as any
api.DELETE = withAuth(_origDelete) as any

export type { paths, components } from './schema'
