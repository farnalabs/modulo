import { computed } from 'vue'
import { useMutation as useTanStackMutation } from '@tanstack/vue-query'
import { formatApiError } from '../lib/api/formatError'

export function useMutation<TInput = void, TOutput = void>(
  fn: (input: TInput) => Promise<{ data?: TOutput; error?: { detail?: string } } | TOutput>,
): { loading: ReturnType<typeof computed<boolean>>; error: ReturnType<typeof computed<string | null>>; mutate: (input: TInput) => Promise<TOutput | undefined> } {
  const mutation = useTanStackMutation({
    mutationFn: async (input: TInput) => {
      const result = await fn(input)
      if (result && typeof result === 'object' && 'data' in result && 'error' in (result as any)) {
        const r = result as { data?: TOutput; error?: { detail?: string } }
        if (r.error) throw new Error(formatApiError(r.error))
        return r.data as TOutput
      }
      return result as TOutput
    },
  })

  return {
    loading: computed(() => mutation.isPending.value),
    error: computed(() => mutation.error.value ? formatApiError(mutation.error.value) : null),
    mutate: async (input: TInput) => {
      try {
        return await mutation.mutateAsync(input)
      } catch {
        return undefined
      }
    },
  }
}
