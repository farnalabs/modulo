import { computed, type ComputedRef } from 'vue'
import { useMutation as useTanStackMutation } from '@tanstack/vue-query'

export function useMutation<TInput = void, TOutput = void>(
  fn: (input: TInput) => Promise<TOutput>,
): { loading: ComputedRef<boolean>; error: ComputedRef<string | null>; mutate: (input: TInput) => Promise<TOutput | undefined> } {
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
