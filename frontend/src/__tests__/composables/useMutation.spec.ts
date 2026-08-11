import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { useMutation } from '../../composables/useMutation'

function mountUseMutation<TInput, TOutput>(fn: (input: TInput) => Promise<TOutput>) {
  let result!: ReturnType<typeof useMutation<TInput, TOutput>>
  mount(defineComponent({
    setup() {
      result = useMutation(fn)
      return () => h('div')
    },
  }))
  return result
}

describe('useMutation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('starts idle with no error', () => {
    const composable = mountUseMutation(async () => 'done')

    expect(composable.loading.value).toBe(false)
    expect(composable.error.value).toBeNull()
  })

  it('returns the mutation output on success', async () => {
    const composable = mountUseMutation(async (input: string) => `echo:${input}`)

    await expect(composable.mutate('hello')).resolves.toBe('echo:hello')
  })

  it('toggles loading while the mutation is in flight', async () => {
    const resolvers: Array<(value: string) => void> = []
    const composable = mountUseMutation(() => new Promise<string>((resolve) => { resolvers.push(resolve) }))

    const pending = composable.mutate(undefined)
    await vi.waitFor(() => expect(resolvers.length).toBe(1))
    expect(composable.loading.value).toBe(true)

    resolvers[0]('ok')
    await pending
    expect(composable.loading.value).toBe(false)
  })

  it('rejects and records the error message when the mutation fails', async () => {
    const composable = mountUseMutation(async () => { throw new Error('mutation exploded') })

    await expect(composable.mutate(undefined)).rejects.toThrow('mutation exploded')
    expect(composable.error.value).toBe('mutation exploded')
    expect(composable.loading.value).toBe(false)
  })

  it('records a generic error message when the error has no message', async () => {
    const composable = mountUseMutation(async () => { throw new Error() })

    await expect(composable.mutate(undefined)).rejects.toThrow()
    expect(composable.error.value).toBe('An error occurred')
  })

  it('records the message of a non-Error rejection', async () => {
    const composable = mountUseMutation(async () => { throw { status: 500 } })

    await expect(composable.mutate(undefined)).rejects.toBeTruthy()
    expect(composable.error.value).toBe('An error occurred')
  })

  it('forwards the input argument to the mutation function', async () => {
    const fn = vi.fn(async (input: { id: number }) => input.id)
    const composable = mountUseMutation(fn)

    await composable.mutate({ id: 123 })
    expect(fn).toHaveBeenCalledWith({ id: 123 })
  })

  it('clears the error before the next mutation', async () => {
    let shouldFail = true
    const composable = mountUseMutation(async () => {
      if (shouldFail) throw new Error('first failed')
      return 'ok'
    })

    await expect(composable.mutate(undefined)).rejects.toThrow()
    expect(composable.error.value).toBe('first failed')

    shouldFail = false
    await composable.mutate(undefined)
    expect(composable.error.value).toBeNull()
  })
})
