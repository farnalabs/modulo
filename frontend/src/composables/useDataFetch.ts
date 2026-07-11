import { ref, onMounted } from 'vue'
import { formatApiError } from '../lib/api/formatError'

interface DataFetchResult<T> {
  loading: ReturnType<typeof ref<boolean>>
  error: ReturnType<typeof ref<string | null>>
  data: ReturnType<typeof ref<T | undefined>>
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
  const loading = ref(false)
  const error = ref<string | null>(null)
  const data = ref<T | undefined>(options?.initialValue)

  async function load() {
    loading.value = true
    error.value = null
    try {
      const result = await fetcher()
      if (result.error) {
        error.value = formatApiError(result.error)
      } else {
        data.value = result.data as T
      }
    } catch (e) {
      error.value = formatApiError(e)
    } finally {
      loading.value = false
    }
  }

  if (options?.immediate !== false) {
    onMounted(() => load())
  }

  return { loading, error, data, load }
}
