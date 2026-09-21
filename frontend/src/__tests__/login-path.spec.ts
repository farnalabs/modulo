import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { resolveLoginPath, resetLoginContextCache } from '../../tests/e2e/setup/login-path'

describe('resolveLoginPath', () => {
  const BASE_URL = 'https://staging.modulo.run'
  const ORIGINAL_ENV = { ...process.env }

  beforeEach(() => {
    resetLoginContextCache()
    vi.restoreAllMocks()
  })

  afterEach(() => {
    process.env = { ...ORIGINAL_ENV }
    resetLoginContextCache()
  })

  it('returns /login when E2E_ORG_SLUG is not set', async () => {
    delete process.env.E2E_ORG_SLUG
    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('returns /login when E2E_ORG_SLUG is empty string', async () => {
    process.env.E2E_ORG_SLUG = ''
    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('returns /login/<slug> when E2E_ORG_SLUG is set and instance is multi-org', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: true, orgs: [{ slug: 'acme' }] }),
    }))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login/acme')
  })

  it('returns /login when E2E_ORG_SLUG is set but instance is single-org (FAR-1123)', async () => {
    process.env.E2E_ORG_SLUG = 'default'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: false }),
    }))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('URL-encodes the slug', async () => {
    process.env.E2E_ORG_SLUG = 'org with spaces'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: true }),
    }))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login/org%20with%20spaces')
  })

  it('caches the login-context response per baseURL', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: true }),
    })
    vi.stubGlobal('fetch', mockFetch)

    await resolveLoginPath(BASE_URL)
    await resolveLoginPath(BASE_URL)
    await resolveLoginPath(BASE_URL)

    expect(mockFetch).toHaveBeenCalledTimes(1)
  })

  it('re-fetches for a different baseURL', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: true }),
    })
    vi.stubGlobal('fetch', mockFetch)

    await resolveLoginPath('https://a.example.com')
    await resolveLoginPath('https://b.example.com')

    expect(mockFetch).toHaveBeenCalledTimes(2)
  })

  it('falls back to /login when fetch fails (network error)', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network error')))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('falls back to /login when fetch returns non-OK', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
    }))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('falls back to /login on fetch timeout', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new DOMException('The operation timed out', 'AbortError')))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('resetCache clears the cache so next call re-fetches', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    const mockFetch = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ multi_org: true }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ multi_org: false }),
      })
    vi.stubGlobal('fetch', mockFetch)

    const path1 = await resolveLoginPath(BASE_URL)
    expect(path1).toBe('/login/acme')

    resetLoginContextCache()

    const path2 = await resolveLoginPath(BASE_URL)
    expect(path2).toBe('/login')
    expect(mockFetch).toHaveBeenCalledTimes(2)
  })

  it('treats multi_org as single-org when value is a string (not strict boolean)', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: 'true' }),
    }))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('treats multi_org as single-org when value is a number', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: 1 }),
    }))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('treats multi_org as single-org when field is missing from response', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ orgs: [{ slug: 'acme' }] }),
    }))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('treats multi_org as single-org when value is null', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: null }),
    }))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })

  it('treats multi_org as single-org when value is an object', async () => {
    process.env.E2E_ORG_SLUG = 'acme'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ multi_org: { enabled: true } }),
    }))

    const path = await resolveLoginPath(BASE_URL)
    expect(path).toBe('/login')
  })
})
