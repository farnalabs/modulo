import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const mockFetch = vi.fn()

describe('BreadcrumbCollector fetch wrapper url extraction (SonarCloud coverage)', () => {
  let originalFetch: unknown

  beforeEach(() => {
    originalFetch = (globalThis as Record<string, unknown>).fetch
    mockFetch.mockReset()
    mockFetch.mockResolvedValue({
      status: 200,
      ok: true,
      json: () => Promise.resolve({}),
    } as unknown as Response)
    ;(globalThis as Record<string, unknown>).fetch = mockFetch
  })

  afterEach(() => {
    ;(globalThis as Record<string, unknown>).fetch = originalFetch
  })

  it('extracts the url from string, URL and Request inputs', async () => {
    const { BreadcrumbCollector } = await import('../lib/error-tracking/breadcrumbs')
    const collector = new BreadcrumbCollector(50)
    collector.startAutoCapture()

    // string input -> url = input
    await (globalThis as Record<string, any>).fetch('https://example.com/string')
    // URL input -> url = input.href
    await (globalThis as Record<string, any>).fetch(new URL('https://example.com/url'))
    // Request-like input -> url = input.url
    await (globalThis as Record<string, any>).fetch({ url: 'https://example.com/request' } as any)

    expect(mockFetch).toHaveBeenCalledTimes(3)
    expect(mockFetch.mock.calls[0][0]).toBe('https://example.com/string')
    expect(mockFetch.mock.calls[1][0]).toBeInstanceOf(URL)
    expect((mockFetch.mock.calls[1][0] as URL).href).toBe('https://example.com/url')
    expect(mockFetch.mock.calls[2][0]).toMatchObject({ url: 'https://example.com/request' })
  })
})
