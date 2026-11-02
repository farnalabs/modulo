import { describe, expect, it } from 'vitest'
import { decodeBase64Url, decodeJwtPayload, principalSubject } from '../lib/jwt'

function tokenWith(payload: Record<string, unknown>): string {
  const encoded = btoa(JSON.stringify(payload))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replaceAll('=', '')
  return `header.${encoded}.signature`
}

describe('decodeBase64Url', () => {
  it('decodes both padded and unpadded base64url segments', () => {
    expect(decodeBase64Url('eyJhIjoxfQ')).toBe('{"a":1}')
    expect(decodeBase64Url('eyJhIjoxfQ==')).toBe('{"a":1}')
  })
})

describe('decodeJwtPayload', () => {
  it('returns the parsed payload for a valid token', () => {
    expect(decodeJwtPayload(tokenWith({ sub: 'user-1' }))).toEqual({ sub: 'user-1' })
  })

  it('returns null for an absent token, an opaque token, or invalid JSON', () => {
    expect(decodeJwtPayload(null)).toBeNull()
    expect(decodeJwtPayload('opaque-token')).toBeNull()
    expect(decodeJwtPayload('header.bm90LWpzb24.signature')).toBeNull()
  })
})

describe('principalSubject', () => {
  it('returns null when the token is absent', () => {
    expect(principalSubject(null)).toBeNull()
    expect(principalSubject(undefined)).toBeNull()
    expect(principalSubject('')).toBeNull()
  })

  it('returns the non-empty string sub claim', () => {
    expect(principalSubject(tokenWith({ sub: 'user-1' }))).toBe('user-1')
  })

  it('returns null when the sub claim is empty or not a string', () => {
    expect(principalSubject(tokenWith({ sub: '' }))).toBeNull()
    expect(principalSubject(tokenWith({ sub: 42 }))).toBeNull()
    expect(principalSubject(tokenWith({}))).toBeNull()
  })
})
