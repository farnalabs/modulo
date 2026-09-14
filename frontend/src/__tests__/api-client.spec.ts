import './helpers/resolve-request-origin'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

// FAR-819: the api client's withAuth wrapper must NOT retry non-idempotent
// (POST/PUT/PATCH/DELETE) requests after a token refresh — only GET is
// retried. This suite exercises the wrapped client (retryable=false) path
// that the useApi tests do not reach, covering the non-retryable branches.
//
// The helpers/resolve-request-origin import MUST come first so globalThis.Request
// and globalThis.fetch are stubbed before lib/api/client's createClient()
// captures them.

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => false),
  clearAccessToken: vi.fn(),
  exitToLogin: vi.fn(),
  wasDemoSessionEnded: vi.fn(() => false),
}))

import {
  attemptTokenRefresh,
  clearAccessToken,
  exitToLogin,
} from '../lib/api/auth'
import { api } from '../lib/api/client'
import { fetchMock } from './helpers/resolve-request-origin'

const mockedAttemptTokenRefresh = attemptTokenRefresh as Mock
const mockedClearAccessToken = clearAccessToken as Mock
const mockedExitToLogin = exitToLogin as Mock

function jsonResponse(status: number): Response {
  return new Response(JSON.stringify({}), { status })
}

beforeEach(() => {
  mockedAttemptTokenRefresh.mockReset()
  mockedAttemptTokenRefresh.mockResolvedValue(false)
  mockedClearAccessToken.mockClear()
  mockedExitToLogin.mockClear()
  fetchMock.mockReset()
  fetchMock.mockImplementation(async () => jsonResponse(401))
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('api client withAuth (non-retryable verbs)', () => {
  it('does not retry a 401 POST and clears the session when refresh fails', async () => {
    mockedAttemptTokenRefresh.mockResolvedValue(false)

    await api.POST('/api/v1/widgets' as never, { body: { name: 'x' } } as never)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(mockedAttemptTokenRefresh).toHaveBeenCalledTimes(1)
    expect(mockedClearAccessToken).toHaveBeenCalledTimes(1)
    expect(mockedExitToLogin).toHaveBeenCalledTimes(1)
  })

  it('does not re-send a 401 POST and leaves the session intact when refresh succeeds', async () => {
    mockedAttemptTokenRefresh.mockResolvedValue(true)

    await api.POST('/api/v1/widgets' as never, { body: { name: 'x' } } as never)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(mockedAttemptTokenRefresh).toHaveBeenCalledTimes(1)
    expect(mockedClearAccessToken).not.toHaveBeenCalled()
    expect(mockedExitToLogin).not.toHaveBeenCalled()
  })

  it('does not retry a 401 PUT/DELETE either', async () => {
    mockedAttemptTokenRefresh.mockResolvedValue(false)

    await api.PUT('/api/v1/widgets/1' as never, { body: { name: 'y' } } as never)
    await api.DELETE('/api/v1/widgets/1' as never, {} as never)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(mockedAttemptTokenRefresh).toHaveBeenCalledTimes(2)
    expect(mockedClearAccessToken).toHaveBeenCalledTimes(2)
  })

  it('retries a 401 GET once after a successful refresh', async () => {
    mockedAttemptTokenRefresh.mockResolvedValue(true)
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401))
      .mockResolvedValueOnce(jsonResponse(200))

    await api.GET('/api/v1/widgets' as never, {} as never)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(mockedAttemptTokenRefresh).toHaveBeenCalledTimes(1)
    expect(mockedClearAccessToken).not.toHaveBeenCalled()
  })
})
