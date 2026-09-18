import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

function okJsonResponse(data: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => data,
  } as unknown as Response
}

beforeEach(() => {
  vi.resetModules()
})

afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('useOrgCurrency', () => {
  it('loads and uppercases the currency code', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJsonResponse({ currency: 'usd' }))
    const { useOrgCurrency } = await import('../../composables/useOrgCurrency')
    const { loadCurrency, currencyCode } = useOrgCurrency()

    expect(await loadCurrency()).toBe('USD')
    expect(currencyCode.value).toBe('USD')
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/org/settings', expect.objectContaining({ method: 'GET' }))
  })

  it('normalises lowercase and whitespace-padded currency codes', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJsonResponse({ currency: '  eur ' }))
    const { useOrgCurrency } = await import('../../composables/useOrgCurrency')
    const { loadCurrency } = useOrgCurrency()

    expect(await loadCurrency()).toBe('EUR')
  })

  it('falls back to USD when the currency is missing', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJsonResponse({}))
    const { useOrgCurrency } = await import('../../composables/useOrgCurrency')
    const { loadCurrency, currencyCode } = useOrgCurrency()

    expect(await loadCurrency()).toBe('USD')
    expect(currencyCode.value).toBe('USD')
  })

  it('falls back to USD when the currency is an empty string', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJsonResponse({ currency: '' }))
    const { useOrgCurrency } = await import('../../composables/useOrgCurrency')
    const { loadCurrency } = useOrgCurrency()

    expect(await loadCurrency()).toBe('USD')
  })

  it('falls back to USD when the currency is not a string', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJsonResponse({ currency: 123 }))
    const { useOrgCurrency } = await import('../../composables/useOrgCurrency')
    const { loadCurrency } = useOrgCurrency()

    expect(await loadCurrency()).toBe('USD')
  })

  it('falls back to USD when the request fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('network down'))
    const { useOrgCurrency } = await import('../../composables/useOrgCurrency')
    const { loadCurrency, currencyCode } = useOrgCurrency()

    expect(await loadCurrency()).toBe('USD')
    expect(currencyCode.value).toBe('USD')
  })

  it('caches the resolved currency and does not re-fetch', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJsonResponse({ currency: 'GBP' }))
    const { useOrgCurrency } = await import('../../composables/useOrgCurrency')
    const { loadCurrency } = useOrgCurrency()

    expect(await loadCurrency()).toBe('GBP')
    expect(await loadCurrency()).toBe('GBP')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('deduplicates concurrent loads into a single request', async () => {
    let resolveFetch!: (value: Response | PromiseLike<Response>) => void
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(() => new Promise<Response>((resolve) => {
        resolveFetch = resolve
      }))
    const { useOrgCurrency } = await import('../../composables/useOrgCurrency')
    const { loadCurrency } = useOrgCurrency()

    const first = loadCurrency()
    const second = loadCurrency()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    resolveFetch(okJsonResponse({ currency: 'JPY' }))
    expect(await first).toBe('JPY')
    expect(await second).toBe('JPY')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('shares module-level currency state across consumers', async () => {
    const { useOrgCurrency } = await import('../../composables/useOrgCurrency')
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(okJsonResponse({ currency: 'AUD' }))
    await useOrgCurrency().loadCurrency()

    const other = useOrgCurrency()
    expect(other.currencyCode.value).toBe('AUD')
  })
})
