import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { Mock } from 'vitest'

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  redirectToLogin: vi.fn(),
}))

import {
  getAuthHeaders,
  attemptTokenRefresh,
  clearAccessToken,
  redirectToLogin,
} from '../lib/api/auth'
import { useApi } from '../composables/useApi'

const mockedGetAuthHeaders = getAuthHeaders as Mock
const mockedAttemptTokenRefresh = attemptTokenRefresh as Mock
const mockedClearAccessToken = clearAccessToken as Mock
const mockedRedirectToLogin = redirectToLogin as Mock

function jsonResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: status === 200 ? 'OK' : 'Error',
    json: vi.fn(async () => body),
  } as unknown as Response
}

let fetchMock: Mock

beforeEach(() => {
  mockedGetAuthHeaders.mockReturnValue({ Authorization: 'Bearer token-1' })
  mockedAttemptTokenRefresh.mockReset()
  mockedAttemptTokenRefresh.mockResolvedValue(true)
  mockedClearAccessToken.mockClear()
  mockedRedirectToLogin.mockClear()
  fetchMock = vi.fn(async () => jsonResponse(200, { id: 1 }))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('useApi request contracts', () => {
  it('GET returns parsed JSON and sends the merged auth header', async () => {
    const api = useApi()
    const data = await api.get<{ id: number }>('/api/v1/widgets')

    expect(data).toEqual({ id: 1 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/widgets',
      expect.objectContaining({
        method: 'GET',
        credentials: 'include',
        headers: expect.objectContaining({ Authorization: 'Bearer token-1' }),
      }),
    )
  })

  it('passes an AbortSignal to every request so timeouts can cancel it', async () => {
    const api = useApi()
    await api.get('/api/v1/widgets')

    const [, init] = fetchMock.mock.calls[0]
    expect((init as RequestInit).signal).toBeInstanceOf(AbortSignal)
  })

  it('serializes JSON bodies for mutating verbs and omits body for GET/DELETE', async () => {
    const api = useApi()

    await api.post('/api/v1/x', { a: 1 })
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/x',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ a: 1 }) }),
    )

    await api.put('/api/v1/x', { a: 2 })
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/x',
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ a: 2 }) }),
    )

    await api.patch('/api/v1/x', { a: 3 })
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/x',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ a: 3 }) }),
    )

    await api.get('/api/v1/x')
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/x',
      expect.objectContaining({ method: 'GET', body: undefined }),
    )

    await api.delete('/api/v1/x')
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/x',
      expect.objectContaining({ method: 'DELETE', body: undefined }),
    )
  })

  it('merges caller headers on top of the default auth headers', async () => {
    const api = useApi()
    await api.get('/api/v1/widgets', { headers: { 'X-Tenant': 'tenant-1' } })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/widgets',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer token-1',
          'X-Tenant': 'tenant-1',
        }),
      }),
    )
  })

  it('does not attach a body when the payload is null', async () => {
    const api = useApi()
    await api.post('/api/v1/x', null)

    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/x',
      expect.objectContaining({ body: undefined }),
    )
  })

  it('returns undefined for 204 No Content responses', async () => {
    fetchMock.mockResolvedValue(jsonResponse(204, undefined))
    const api = useApi()

    await expect(api.delete('/api/v1/widgets/1')).resolves.toBeUndefined()
    expect(fetchMock.mock.results[0].value).toBeInstanceOf(Promise)
  })
})

describe('useApi 401 refresh flow', () => {
  it('refreshes once and retries the request with the new token', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { detail: 'token expired' }))
      .mockResolvedValueOnce(jsonResponse(200, { id: 7 }))
    mockedGetAuthHeaders
      .mockReturnValueOnce({ Authorization: 'Bearer token-1' })
      .mockReturnValueOnce({ Authorization: 'Bearer token-2' })
    mockedAttemptTokenRefresh.mockResolvedValue(true)

    const api = useApi()
    const data = await api.get<{ id: number }>('/api/v1/widgets')

    expect(mockedAttemptTokenRefresh).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/widgets',
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer token-2' }),
      }),
    )
    expect(data).toEqual({ id: 7 })
  })

  it('clears the session and redirects to login when the refresh fails', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'token expired' }))
    mockedAttemptTokenRefresh.mockResolvedValue(false)

    const api = useApi()
    await expect(api.get('/api/v1/widgets')).rejects.toThrow(
      'Session expired. Please log in again.',
    )

    expect(mockedClearAccessToken).toHaveBeenCalledTimes(1)
    expect(mockedRedirectToLogin).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(mockedAttemptTokenRefresh).toHaveBeenCalledTimes(1)
  })

  it('gives up when the retried request is still 401', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'token expired' }))
    mockedAttemptTokenRefresh.mockResolvedValue(true)

    const api = useApi()
    await expect(api.get('/api/v1/widgets')).rejects.toThrow(
      'Session expired. Please log in again.',
    )

    expect(mockedAttemptTokenRefresh).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(mockedClearAccessToken).toHaveBeenCalledTimes(1)
    expect(mockedRedirectToLogin).toHaveBeenCalledTimes(1)
  })

  it('does not attempt a refresh on non-401 errors', async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'forbidden' }))

    const api = useApi()
    await expect(api.get('/api/v1/widgets')).rejects.toThrow('forbidden')

    expect(mockedAttemptTokenRefresh).not.toHaveBeenCalled()
    expect(mockedClearAccessToken).not.toHaveBeenCalled()
  })
})

describe('useApi error mapping', () => {
  it('surfaces the API detail message on non-2xx responses', async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, { detail: 'name is required' }))

    const api = useApi()
    await expect(api.get('/api/v1/widgets')).rejects.toThrow('name is required')
  })

  it('collapses FastAPI array-typed 422 detail into a readable message', async () => {
    // A Pydantic 422 arrives as { detail: [{loc, msg, type}, ...] }. The Error
    // thrown by useApi must carry the joined msg text — not "[object Object]"
    // (what `new Error(array)` stringifies to). Fails without the formatApiError
    // call in request().
    fetchMock.mockResolvedValue(
      jsonResponse(422, {
        detail: [
          { loc: ['body', 'stages', 1, 'id'], msg: 'lifecycle-map stage #1: duplicate stage id', type: 'value_error' },
        ],
      }),
    )

    const api = useApi()
    await expect(api.get('/api/v1/widgets')).rejects.toThrow(
      'lifecycle-map stage #1: duplicate stage id',
    )
  })

  it('falls back to the HTTP status text when the error body is not JSON', async () => {
    fetchMock.mockResolvedValue({
      status: 500,
      ok: false,
      statusText: 'Internal Server Error',
      json: vi.fn(async () => {
        throw new SyntaxError('Unexpected token < in JSON')
      }),
    } as unknown as Response)

    const api = useApi()
    await expect(api.get('/api/v1/widgets')).rejects.toThrow('Internal Server Error')
  })

  it('propagates network-level fetch rejections', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    const api = useApi()
    await expect(api.get('/api/v1/widgets')).rejects.toThrow('Failed to fetch')
  })
})

describe('useApi timeout', () => {
  it('aborts in-flight requests after the 30s request timeout', async () => {
    vi.useFakeTimers()
    fetchMock.mockImplementation(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          const signal = init?.signal
          if (!signal) {
            reject(new Error('no signal provided'))
            return
          }
          if (signal.aborted) {
            reject(new DOMException('The operation was aborted.', 'AbortError'))
            return
          }
          signal.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          )
        }),
    )

    const api = useApi()
    const pending = api.get('/api/v1/widgets')
    const assertion = expect(pending).rejects.toThrow('The operation was aborted.')

    await vi.advanceTimersByTimeAsync(30_000)
    await assertion
  })

  it('completes normally when the request settles before the timeout', async () => {
    vi.useFakeTimers()
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 1 }))

    const api = useApi()
    const pending = api.get('/api/v1/widgets')

    await vi.advanceTimersByTimeAsync(1_000)
    await expect(pending).resolves.toEqual({ id: 1 })
  })
})
