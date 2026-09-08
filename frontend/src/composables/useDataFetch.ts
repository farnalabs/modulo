import { computed, ref, type ComputedRef, type WritableComputedRef } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { formatApiError } from '../lib/api/formatError'

let keyCounter = 0

interface DataFetchResult<T> {
  loading: ComputedRef<boolean>
  error: WritableComputedRef<string | null>
  data: WritableComputedRef<T | undefined>
  fetched: ComputedRef<boolean>
  load: () => Promise<void>
}

interface InitializedDataFetchResult<T> extends Omit<DataFetchResult<T>, 'data'> {
  data: WritableComputedRef<T>
}

interface DataFetchOptions<T> {
  initialValue?: T
  immediate?: boolean
  /**
   * FAR-691: when true, only the FIRST load flips `loading`; subsequent
   * refetches update `data` in place without flipping it, so a rendered
   * list stays mounted across auto-refresh / filter refetches (the expanded
   * HITL gate card keeps its notes textarea focused). Refetch errors still
   * surface through the existing error channel. Default false — other
   * consumers keep today's behaviour.
   */
  silentRefetch?: boolean
}

type FetcherResult<T> = { data?: T; error?: { detail?: unknown } }

export function useDataFetch<T>(
  fetcher: () => Promise<FetcherResult<T>> | FetcherResult<T>,
  options: DataFetchOptions<T> & { initialValue: T },
): InitializedDataFetchResult<T>
export function useDataFetch<T>(
  fetcher: () => Promise<FetcherResult<T>> | FetcherResult<T>,
  options?: DataFetchOptions<T>,
): DataFetchResult<T>
export function useDataFetch<T>(
  fetcher: () => Promise<FetcherResult<T>> | FetcherResult<T>,
  options?: DataFetchOptions<T>,
): DataFetchResult<T> {
  const key = [`useDataFetch`, ++keyCounter]
  const queryClient = useQueryClient()
  const fetched = ref(false)
  const errorOverride = ref<string | null>(null)
  const silentRefetch = options?.silentRefetch ?? false

  const { data, error, isLoading, isFetching, refetch } = useQuery<T, Error>({
    queryKey: key,
    queryFn: async () => {
      const result = await fetcher()
      if (result.error) throw new Error(formatApiError(result.error))
      fetched.value = true
      return result.data as T
    },
    enabled: options?.immediate !== false,
    retry: false,
    staleTime: 30_000,
  })

  return {
    // silentRefetch (FAR-691): `isLoading` is true only while the query has
    // no data yet (the first load — or a first load that failed), so
    // refetches of an already-populated query never flip `loading`.
    loading: computed(() => (silentRefetch ? isLoading.value : isLoading.value || isFetching.value)),
    error: computed({
      get: () => errorOverride.value ?? (error.value ? (error.value.message ?? 'An error occurred') : null),
      set: value => { errorOverride.value = value },
    }),
    data: computed({
      get: () => data.value ?? options?.initialValue,
      set: value => queryClient.setQueryData<T | undefined>(key, value),
    }),
    fetched: computed(() => fetched.value),
    load: async () => {
      errorOverride.value = null
      await refetch()
    },
  }
}
