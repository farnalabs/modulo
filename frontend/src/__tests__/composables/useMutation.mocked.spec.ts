import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref } from 'vue'

const h = vi.hoisted(() => {
  const mutationFn: { value: ((input: unknown) => Promise<unknown>) | null } = { value: null }
  return { mutationFn }
})

const isPending = ref(false)
const error = ref<Error | null>(null)
const mutateAsync = vi.fn(async (input: unknown) => {
  if (!h.mutationFn.value) return input
  return h.mutationFn.value(input)
})

vi.mock('@tanstack/vue-query', () => ({
  useMutation: (opts: { mutationFn: (input: unknown) => Promise<unknown> }) => {
    h.mutationFn.value = opts.mutationFn
    return {
      isPending,
      error,
      mutateAsync,
    }
  },
  useQueryClient: () => ({ setQueryData: vi.fn() }),
  useQuery: () => ({
    data: ref(undefined),
    error: ref(null),
    isLoading: ref(false),
    isFetching: ref(false),
    refetch: vi.fn(),
  }),
}))

beforeEach(() => {
  vi.clearAllMocks()
  isPending.value = false
  error.value = null
  h.mutationFn.value = null
})

async function setupMutation<TInput = void, TOutput = void>(fn: (input: TInput) => Promise<TOutput>) {
  const { useMutation } = await import('../../composables/useMutation')
  return useMutation<TInput, TOutput>(fn)
}

describe('useMutation', () => {
  it('mutate resolves with the output of the mutation function', async () => {
    const mutate = await setupMutation<number, number>(async (n) => n * 2)

    await expect(mutate.mutate(21)).resolves.toBe(42)
    expect(mutateAsync).toHaveBeenCalledWith(21)
  })

  it('loading is false before and after a mutation', async () => {
    const mutate = await setupMutation<number, number>(async (n) => n)

    expect(mutate.loading.value).toBe(false)
    await mutate.mutate(1)
    expect(mutate.loading.value).toBe(false)
  })

  it('loading reflects the pending state', async () => {
    const mutate = await setupMutation<number, number>(async (n) => n)
    isPending.value = true
    expect(mutate.loading.value).toBe(true)
    isPending.value = false
    expect(mutate.loading.value).toBe(false)
  })

  it('error is null when the mutation succeeds', async () => {
    const mutate = await setupMutation<number, number>(async (n) => n)
    await mutate.mutate(1)
    expect(mutate.error.value).toBeNull()
  })

  it('surfaces the mutation error message', async () => {
    const mutate = await setupMutation<number, number>(async (n) => n)
    error.value = new Error('mutation failed')
    expect(mutate.error.value).toBe('mutation failed')
  })

  it('falls back to a generic message when the error has no message', async () => {
    const mutate = await setupMutation<number, number>(async (n) => n)
    error.value = { message: null } as unknown as Error
    expect(mutate.error.value).toBe('An error occurred')
  })

  it('propagates a rejected mutation to the caller', async () => {
    const mutate = await setupMutation<number, number>(async () => {
      throw new Error('nope')
    })

    await expect(mutate.mutate(1)).rejects.toThrow('nope')
  })

  it('passes the input through to the mutation function', async () => {
    const fn = vi.fn(async (input: { id: string }) => input)
    const mutate = await setupMutation<{ id: string }, { id: string }>(fn)

    await mutate.mutate({ id: 'abc' })
    expect(fn).toHaveBeenCalledWith({ id: 'abc' })
  })
})
