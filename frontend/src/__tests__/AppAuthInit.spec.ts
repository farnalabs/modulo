import { describe, it, expect } from 'vitest'
import { getInitialAuthState, shouldReRunAutoLogin } from '../lib/api/client'

describe('App auth-state initialization', () => {
  it('starts authenticated when a token already exists, even with auto-login configured', () => {
    expect(getInitialAuthState(true)).toBe(true)
  })

  it('starts unauthenticated when no token exists, with auto-login configured', () => {
    expect(getInitialAuthState(false)).toBe(false)
  })

  it('starts unauthenticated when no token exists and no auto-login configured', () => {
    expect(getInitialAuthState(false)).toBe(false)
  })
})

describe('auto-login recovery on session clear', () => {
  it('re-runs auto-login when an authenticated session clears with auto-login configured', () => {
    expect(shouldReRunAutoLogin(true, false, true)).toBe(true)
  })

  it('does not re-run auto-login when a session clears without auto-login configured', () => {
    expect(shouldReRunAutoLogin(true, false, false)).toBe(false)
  })

  it('does not re-run auto-login on a fresh unauthenticated start', () => {
    expect(shouldReRunAutoLogin(false, false, true)).toBe(false)
  })

  it('does not re-run auto-login when still authenticated', () => {
    expect(shouldReRunAutoLogin(true, true, true)).toBe(false)
  })
})
