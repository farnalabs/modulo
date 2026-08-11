import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref, type Ref } from 'vue'

const h = vi.hoisted(() => {
  const setQueryData = vi.fn()
  const refetch = vi.fn(() => Promise.resolve())
  const capturedQueryFn: { value: ((() => Promise<unknown>) | null) } = { value: null }
  return { setQueryData, refetch, capturedQueryFn }
})

const data = ref<unknown>(undefined)
const error = ref<Error | null>(null)
const isLoading = ref(false)
const isFetching = ref(false)

vi.mock('@tanstack/vue-query', () => ({
  useQueryClient: () => ({ setQueryData: h.setQueryData }),
  useQuery: (opts: { queryFn: () => Promise<unknown> }) => {
    h.capturedQueryFn.value = opts.queryFn
    return {
      data,
      error,
      isLoading,
      isFetching,
      refetch: h.refetch,
    }
  },
  useMutation: () => ({ mutateAsync: vi.fn() }),
}))

beforeEach(() => {
  vi.clearAllMocks()
  data.value = undefined
  error.value = null
  isLoading.value = false
  isFetching.value = false
  h.capturedQueryFn.value = null
  h.refetch.mockResolvedValue(undefined)
})

async function setupFetch<T>(
  fetcher: () => Promise<{ data?: T; error?: { detail?: unknown } }>,
  options?: Parameters<typeof useDataFetch<T>>[1],
) {
  const { useDataFetch } = await import('../../composables/useDataFetch')
  return useDataFetch(fetcher, options)
}

describe('useDataFetch queryFn contract', () => {
  it('returns data and marks fetched when the fetcher succeeds', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 42 }), { initialValue: 0 })

    const result = await h.capturedQueryFn.value!()
    expect(result).toBe(42)
    expect(df.fetched.value).toBe(true)
  })

  it('throws a formatted error when the fetcher returns an error payload', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ error: { detail: 'nope' } }), { initialValue: 0 })

    await expect(h.capturedQueryFn.value!()).rejects.toThrow('nope')
    expect(df.fetched.value).toBe(false)
  })

  it('does not mark fetched when the fetcher rejects', async () => {
    const df = await setupFetch<number>(() => Promise.reject(new Error('boom')), { initialValue: 0 })

    await expect(h.capturedQueryFn.value!()).rejects.toThrow('boom')
    expect(df.fetched.value).toBe(false)
  })

  it('forwards non-detail error shapes through formatApiError', async () => {
    await setupFetch<number>(() => Promise.resolve({ error: { message: 'kaboom' } }), { initialValue: 0 })

    await expect(h.capturedQueryFn.value!()).rejects.toThrow('kaboom')
  })
})

describe('useDataFetch data & error refs', () => {
  it('falls back to initialValue while no data has loaded', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }), { initialValue: 7 })
    expect(df.data.value).toBe(7)
  })

  it('returns undefined data without an initialValue', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }))
    expect(df.data.value).toBeUndefined()
  })

  it('surfaces the query error message', async () => {
    error.value = new Error('query failed')
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }), { initialValue: 0 })
    expect(df.error.value).toBe('query failed')
  })

  it('falls back to a generic message when the query error has no message', async () => {
    error.value = { message: null } as unknown as Error
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }), { initialValue: 0 })
    expect(df.error.value).toBe('An error occurred')
  })

  it('is null when there is no query error', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }), { initialValue: 0 })
    expect(df.error.value).toBeNull()
  })

  it('lets callers override the error message via the writable ref', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }), { initialValue: 0 })
    df.error.value = 'overridden'
    expect(df.error.value).toBe('overridden')
  })

  it('load() clears a caller error override before refetching', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }), { initialValue: 0 })
    df.error.value = 'stale'
    expect(df.error.value).toBe('stale')

    await df.load()
    expect(df.error.value).toBeNull()
    expect(h.refetch).toHaveBeenCalledTimes(1)
  })

  it('data writes propagate to the query client cache', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }), { initialValue: 0 })
    df.data.value = 99
    expect(h.setQueryData).toHaveBeenCalledTimes(1)
    const [key, value] = h.setQueryData.mock.calls[0]
    expect(key).toHaveLength(2)
    expect(key[0]).toBe('useDataFetch')
    expect(value).toBe(99)
  })

  it('loading reflects both isLoading and isFetching', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }), { initialValue: 0 })
    expect(df.loading.value).toBe(false)

    isLoading.value = true
    expect(df.loading.value).toBe(true)

    isLoading.value = false
    isFetching.value = true
    expect(df.loading.value).toBe(true)

    isFetching.value = false
    expect(df.loading.value).toBe(false)
  })

  it('fetched starts false and flips true once data lands', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 1 }), { initialValue: 0 })
    expect(df.fetched.value).toBe(false)

    await h.capturedQueryFn.value!()
    expect(df.fetched.value).toBe(true)
  })

  it('serves initialValue until query data resolves, then serves the data', async () => {
    const df = await setupFetch<number>(() => Promise.resolve({ data: 5 }), { initialValue: 3 })
    expect(df.data.value).toBe(3)

    data.value = 5
    expect(df.data.value).toBe(5)
  })
})
