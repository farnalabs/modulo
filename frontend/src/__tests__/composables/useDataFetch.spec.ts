import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { useDataFetch } from '../../composables/useDataFetch'

type FetcherResult<T> = { data?: T; error?: { detail?: unknown } }

function mountUseDataFetch<T>(
  fetcher: () => Promise<FetcherResult<T>> | FetcherResult<T>,
  options?: Parameters<typeof useDataFetch<T>>[1],
) {
  let result!: ReturnType<typeof useDataFetch<T>>
  mount(defineComponent({
    setup() {
      result = useDataFetch(fetcher, options)
      return () => h('div')
    },
  }))
  return result
}

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

describe('useDataFetch', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('starts in a loading state with no data, error, or fetched flag', async () => {
    const fetcher = vi.fn(() => new Promise<FetcherResult<number>>(() => {}))
    const composable = mountUseDataFetch(fetcher)

    expect(composable.loading.value).toBe(true)
    expect(composable.error.value).toBeNull()
    expect(composable.fetched.value).toBe(false)
    expect(composable.data.value).toBeUndefined()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('populates data and flips fetched when the fetcher resolves', async () => {
    const composable = mountUseDataFetch(async () => ({ data: 42 }))

    await vi.waitFor(() => expect(composable.data.value).toBe(42))
    expect(composable.fetched.value).toBe(true)
    expect(composable.loading.value).toBe(false)
    expect(composable.error.value).toBeNull()
  })

  it('uses initialValue as the data fallback before the fetch resolves', async () => {
    const fetcher = vi.fn(() => new Promise<FetcherResult<number>>(() => {}))
    const composable = mountUseDataFetch(fetcher, { initialValue: 7 })

    expect(composable.data.value).toBe(7)
    expect(composable.fetched.value).toBe(false)
  })

  it('surfaces the formatted error and keeps fetched false when the API returns an error result', async () => {
    const composable = mountUseDataFetch(async () => ({ error: { detail: 'Audit endpoint unavailable' } }))

    await vi.waitFor(() => expect(composable.error.value).toBe('Audit endpoint unavailable'))
    expect(composable.fetched.value).toBe(false)
    expect(composable.data.value).toBeUndefined()
    expect(composable.loading.value).toBe(false)
  })

  it('surfaces the error message when the fetcher rejects', async () => {
    const composable = mountUseDataFetch(async () => { throw new Error('network down') })

    await vi.waitFor(() => expect(composable.error.value).toBe('network down'))
    expect(composable.fetched.value).toBe(false)
  })

  it('does not fetch automatically when immediate is false', async () => {
    const fetcher = vi.fn(() => new Promise<FetcherResult<number>>(() => {}))
    const composable = mountUseDataFetch(fetcher, { immediate: false })

    expect(fetcher).not.toHaveBeenCalled()
    expect(composable.loading.value).toBe(false)
    expect(composable.fetched.value).toBe(false)
  })

  it('fetches on demand via load when immediate is false', async () => {
    const composable = mountUseDataFetch(async () => ({ data: 'loaded' }), { immediate: false })

    await composable.load()
    await vi.waitFor(() => expect(composable.data.value).toBe('loaded'))
    expect(composable.fetched.value).toBe(true)
  })

  it('load clears a prior error override before refetching', async () => {
    let shouldFail = true
    const composable = mountUseDataFetch(async () => {
      if (shouldFail) return { error: { detail: 'first attempt failed' } }
      return { data: 'recovered' }
    })

    await vi.waitFor(() => expect(composable.error.value).toBe('first attempt failed'))

    shouldFail = false
    await composable.load()
    await vi.waitFor(() => expect(composable.data.value).toBe('recovered'))
    expect(composable.error.value).toBeNull()
  })

  it('load refetches and updates data', async () => {
    let value = 1
    const composable = mountUseDataFetch(async () => ({ data: value }))

    await vi.waitFor(() => expect(composable.data.value).toBe(1))

    value = 2
    await composable.load()
    await vi.waitFor(() => expect(composable.data.value).toBe(2))
  })

  it('exposes a writable data setter that writes back to the query cache', async () => {
    const composable = mountUseDataFetch(async () => ({ data: 1 }))
    await vi.waitFor(() => expect(composable.data.value).toBe(1))

    composable.data.value = 99
    expect(composable.data.value).toBe(99)
  })

  it('exposes a writable error setter that overrides the computed error', async () => {
    const composable = mountUseDataFetch(async () => ({ data: 'ok' }))
    await vi.waitFor(() => expect(composable.data.value).toBe('ok'))

    composable.error.value = 'dismissed locally'
    expect(composable.error.value).toBe('dismissed locally')
  })

  it('clears the error override on a successful load', async () => {
    const composable = mountUseDataFetch(async () => ({ data: 'ok' }))
    await vi.waitFor(() => expect(composable.data.value).toBe('ok'))

    composable.error.value = 'dismissed locally'
    await composable.load()
    await settle()

    expect(composable.error.value).toBeNull()
  })

  it('uses the fetcher-provided data even when initialValue is present', async () => {
    const composable = mountUseDataFetch(async () => ({ data: 'real' }), { initialValue: 'stale' })

    await vi.waitFor(() => expect(composable.data.value).toBe('real'))
  })
})
