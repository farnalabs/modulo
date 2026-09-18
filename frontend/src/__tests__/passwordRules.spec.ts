import { describe, it, expect } from 'vitest'
import {
  PASSWORD_MIN_LENGTH,
  validatePasswordClient,
  passwordRuleKey,
  type PasswordRuleCode,
} from '../lib/passwordRules'

describe('passwordRules', () => {
  it('exposes the shared minimum length', () => {
    expect(PASSWORD_MIN_LENGTH).toBeGreaterThanOrEqual(8)
  })

  it('rejects passwords shorter than the minimum', () => {
    expect(validatePasswordClient('Ab1')).toBe('too_short')
  })

  it('rejects passwords missing an uppercase character', () => {
    expect(validatePasswordClient('abcd1234!')).toBe('missing_uppercase')
  })

  it('rejects passwords missing a lowercase character', () => {
    expect(validatePasswordClient('ABCD1234!')).toBe('missing_lowercase')
  })

  it('rejects passwords missing a digit', () => {
    expect(validatePasswordClient('Abcdefgh!')).toBe('missing_digit')
  })

  it('accepts passwords satisfying every client rule', () => {
    expect(validatePasswordClient('C0rr3ct-Horse-Battery')).toBeNull()
  })

  it('maps every rule code to its i18n key', () => {
    const codes: PasswordRuleCode[] = ['too_short', 'missing_uppercase', 'missing_lowercase', 'missing_digit']
    for (const code of codes) {
      expect(passwordRuleKey(code)).toBe(`common.password_rules.${code}`)
    }
  })
})
