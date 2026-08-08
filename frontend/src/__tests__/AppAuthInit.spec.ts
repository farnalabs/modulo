import { describe, it, expect } from 'vitest'
import { getInitialAuthState } from '../lib/api/client'

describe('App auth-state initialization', () => {
  it('starts authenticated when a token already exists, even with auto-login configured', () => {
    expect(getInitialAuthState(true, true)).toBe(true)
  })

  it('starts unauthenticated when no token exists, with auto-login configured', () => {
    expect(getInitialAuthState(false, true)).toBe(false)
  })

  it('starts unauthenticated when no token exists and no auto-login configured', () => {
    expect(getInitialAuthState(false, false)).toBe(false)
  })
})
