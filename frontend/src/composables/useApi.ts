const BASE = ''

const TOKEN_KEY = 'modulo_access_token'

interface ApiOptions {
  headers?: Record<string, string>
}

function authHeader(): Record<string, string> {
  try {
    const token = localStorage.getItem(TOKEN_KEY)
    return token ? { Authorization: `Bearer ${token}` } : {}
  } catch {
    return {}
  }
}

async function request<T>(method: string, path: string, body?: unknown, options?: ApiOptions): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
      ...options?.headers,
    },
    body: body != null ? JSON.stringify(body) : undefined,
    credentials: 'include',
  })
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
