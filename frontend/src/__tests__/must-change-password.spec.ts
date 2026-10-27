import { beforeEach, describe, expect, it, vi } from 'vitest'

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }))

vi.mock('../lib/api/client', () => ({
  api: { GET: getMock },
}))

import {
  applyMustChangePassword,
  setMustChangePassword,
  syncFromMe,
  useMustChangePassword,
} from '../lib/mustChangePassword'

describe('must-change-password gate', () => {
  beforeEach(() => {
    getMock.mockReset()
    applyMustChangePassword(false)
  })

  it('shares reactive state and accepts direct boolean state changes', () => {
    const state = useMustChangePassword()
    expect(state.value).toBe(false)

    setMustChangePassword(true)
    expect(useMustChangePassword().value).toBe(true)

    setMustChangePassword(false)
    expect(state.value).toBe(false)
  })

  it('arms only for the literal true payload', () => {
    for (const value of [null, undefined, 1, 'true', {}, false]) {
      applyMustChangePassword(value)
      expect(useMustChangePassword().value).toBe(false)
    }

    applyMustChangePassword(true)
    expect(useMustChangePassword().value).toBe(true)
  })

  it('applies the /me flag and clears the gate on API errors or thrown failures', async () => {
    applyMustChangePassword(false)
    getMock.mockResolvedValueOnce({ data: { must_change_password: true } })
    await syncFromMe()
    expect(useMustChangePassword().value).toBe(true)

    getMock.mockResolvedValueOnce({ error: { detail: 'expired token' } })
    await syncFromMe()
    expect(useMustChangePassword().value).toBe(false)

    applyMustChangePassword(true)
    getMock.mockRejectedValueOnce(new Error('network unavailable'))
    await syncFromMe()
    expect(useMustChangePassword().value).toBe(false)
  })
})
