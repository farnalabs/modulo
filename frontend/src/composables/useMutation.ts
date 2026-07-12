import { computed } from 'vue'
import { useMutation as useTanStackMutation } from '@tanstack/vue-query'

export function useMutation<TInput = void, TOutput = void>(
  fn: (input: TInput) => Promise<TOutput>,
): { loading: ReturnType<typeof computed<boolean>>; error: ReturnType<typeof computed<string | null>>; mutate: (input: TInput) => Promise<TOutput | undefined> } {
  const mutation = useTanStackMutation({
    mutationFn: async (input: TInput) => {
      return await fn(input)
    },
  })

  return {
    loading: computed(() => mutation.isPending.value),
    error: computed(() => mutation.error.value ? (mutation.error.value?.message ?? 'An error occurred') : null),
    mutate: async (input: TInput) => {
      return await mutation.mutateAsync(input)
    },
  }
}
