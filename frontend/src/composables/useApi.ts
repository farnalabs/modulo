import {
  getAuthHeaders,
  attemptTokenRefresh,
  clearAccessToken,
  redirectToLogin,
} from '../lib/api/auth'

const BASE = ''

interface ApiOptions {
  headers?: Record<string, string>
}

const REQUEST_TIMEOUT_MS = 30000

async function requestWorker(method: string, path: string, body?: unknown, options?: ApiOptions): Promise<Response> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const headers = {
      'Content-Type': 'application/json',
      ...getAuthHeaders(),
      ...options?.headers,
    }
    const res = await fetch(`${BASE}${path}`, {
      method,
      signal: controller.signal,
      headers,
      body: body != null ? JSON.stringify(body) : undefined,
      credentials: 'include',
    })
    return res
  } finally {
    clearTimeout(timer)
  }
}

async function request<T>(method: string, path: string, body?: unknown, options?: ApiOptions): Promise<T> {
  let res = await requestWorker(method, path, body, options)

  if (res.status === 401) {
    const refreshed = await attemptTokenRefresh()
    if (refreshed) {
      res = await requestWorker(method, path, body, options)
    }
    if (!refreshed || res.status === 401) {
      clearAccessToken()
      redirectToLogin()
      throw new Error('Session expired. Please log in again.')
    }
  }

  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(detail.detail ?? `Request failed: ${res.status}`)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export function useApi() {
  return {
    get: <T>(path: string, options?: ApiOptions) => request<T>('GET', path, undefined, options),
    post: <T>(path: string, body?: unknown, options?: ApiOptions) => request<T>('POST', path, body, options),
    put: <T>(path: string, body?: unknown, options?: ApiOptions) => request<T>('PUT', path, body, options),
    patch: <T>(path: string, body?: unknown, options?: ApiOptions) => request<T>('PATCH', path, body, options),
    delete: <T>(path: string, options?: ApiOptions) => request<T>('DELETE', path, undefined, options),
  }
}
