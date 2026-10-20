import { describe, expect, it } from 'vitest'
import { formatApiError } from '../lib/api/formatError'

function circular(): Record<string, unknown> {
  const obj: Record<string, unknown> = {}
  obj.self = obj
  return obj
}

describe('formatError', () => {
  it('surfaces a ProblemDetail detail', () => {
    expect(
      formatApiError({
        type: 'urn:problem:modulo:not_found',
        title: 'Not Found',
        status: 404,
        detail: 'missing',
      }),
    ).toBe('missing')
  })

  it('falls back to Unknown error when the detail object cannot be serialized', () => {
    // A circular reference makes JSON.stringify throw, exercising the
    // serialize-on-failure fallback path in stringifyErrorObject.
    expect(formatApiError(circular())).toBe('Unknown error')
  })

  it('prefers an explicit detail string on a non-ProblemDetail object', () => {
    expect(formatApiError({ detail: 'boom', title: 'x' })).toBe('boom')
  })

  it('surfaces a raw string error directly', () => {
    expect(formatApiError('plain string error')).toBe('plain string error')
  })

  it('surfaces an Error message directly', () => {
    expect(formatApiError(new Error('boom message'))).toBe('boom message')
  })

  it('stringifies truthy primitives and functions without object coercion', () => {
    expect(formatApiError(42)).toBe('42')
    expect(formatApiError(10n)).toBe('10')
    expect(formatApiError(Symbol('sym'))).toBe('Symbol(sym)')
    expect(formatApiError(true)).toBe('true')
    expect(formatApiError(function namedFn() { return 1 })).toContain('namedFn')
  })

  it('returns Unknown error for falsy non-string values', () => {
    expect(formatApiError(null)).toBe('Unknown error')
    expect(formatApiError(undefined)).toBe('Unknown error')
    expect(formatApiError(0)).toBe('Unknown error')
    expect(formatApiError(false)).toBe('Unknown error')
  })
})
