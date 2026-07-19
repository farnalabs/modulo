import { afterEach, describe, expect, it } from 'vitest'
import { getAutoLoginConfig } from '../config/runtime'

describe('getAutoLoginConfig', () => {
  afterEach(() => {
    delete window.__MODULO_CONFIG__
  })

  it('does not enable auto-login when runtime credentials are absent', () => {
    window.__MODULO_CONFIG__ = {}

    expect(getAutoLoginConfig()).toBeUndefined()
  })

  it('returns explicitly configured demo credentials', () => {
    window.__MODULO_CONFIG__ = {
      autoLogin: { username: 'demo', password: 'demo' },
    }

    expect(getAutoLoginConfig()).toEqual({ username: 'demo', password: 'demo' })
  })

  it('rejects incomplete or non-string credentials', () => {
    window.__MODULO_CONFIG__ = {
      autoLogin: { username: 'demo', password: undefined },
    }

    expect(getAutoLoginConfig()).toBeUndefined()
  })
})
