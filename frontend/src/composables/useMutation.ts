import { ref } from 'vue'
import { formatApiError } from '../lib/api/formatError'

interface UseMutationResult<TInput, TOutput> {
  loading: ReturnType<typeof ref<boolean>>
  error: ReturnType<typeof ref<string | null>>
  mutate: (input: TInput) => Promise<TOutput | undefined>
}

export function useMutation<TInput = void, TOutput = void>(
  fn: (input: TInput) => Promise<{ data?: TOutput; error?: { detail?: string } } | TOutput>,
): UseMutationResult<TInput, TOutput> {
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function mutate(input: TInput): Promise<TOutput | undefined> {
    loading.value = true
    error.value = null
    try {
      const result = await fn(input)
      if (result && typeof result === 'object' && 'data' in result && 'error' in (result as any)) {
        const r = result as { data?: TOutput; error?: { detail?: string } }
        if (r.error) {
          error.value = formatApiError(r.error)
          return undefined
        }
        return r.data
      }
      return result as TOutput
    } catch (e) {
      error.value = formatApiError(e)
      return undefined
    } finally {
      loading.value = false
    }
  }

  return { loading, error, mutate }
}
