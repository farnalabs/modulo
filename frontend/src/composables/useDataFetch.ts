import { computed, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { formatApiError } from '../lib/api/formatError'

let keyCounter = 0

interface DataFetchResult<T> {
  loading: ReturnType<typeof computed<boolean>>
  error: ReturnType<typeof computed<string | null>>
  data: ReturnType<typeof computed<T | undefined>>
  fetched: ReturnType<typeof computed<boolean>>
  load: () => Promise<void>
}

interface DataFetchOptions<T> {
  initialValue?: T
  immediate?: boolean
}

type FetcherResult<T> = { data?: T; error?: { detail?: string } }

export function useDataFetch<T>(
  fetcher: () => Promise<FetcherResult<T>> | FetcherResult<T>,
  options?: DataFetchOptions<T>,
): DataFetchResult<T> {
  const key = [`useDataFetch`, ++keyCounter]

  const fetched = ref(false)

  const { data, error, isLoading, isFetching, refetch } = useQuery({
    queryKey: key,
    queryFn: async () => {
      const result = await fetcher()
      if (result.error) throw new Error(formatApiError(result.error))
      fetched.value = true
      return result.data as T
    },
    enabled: options?.immediate !== false,
    initialData: options?.initialValue,
    retry: 1,
    staleTime: 30_000,
  })

  return {
    loading: computed(() => isLoading.value || isFetching.value),
    error: computed(() => error.value ? (error.value?.message ?? 'An error occurred') : null),
    data: computed(() => data.value),
    fetched: computed(() => fetched.value),
    load: async () => { await refetch() },
  }
}
