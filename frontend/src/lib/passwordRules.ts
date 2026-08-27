export type PasswordRuleCode = 'too_short' | 'missing_uppercase' | 'missing_lowercase' | 'missing_digit'

export const PASSWORD_MIN_LENGTH = 8

/**
 * Single source of truth for client-side password rules (FAR-461).
 *
 * Mirrors the backend `validate_password_strength` contract: minimum length
 * plus at least one uppercase letter, one lowercase letter, and one digit.
 * Returns the FIRST failing rule as a stable error-code string, or null when
 * the password satisfies every rule.
 */
export function validatePasswordClient(password: string): PasswordRuleCode | null {
  if (password.length < PASSWORD_MIN_LENGTH) {
    return 'too_short'
  }
  if (!/[A-Z]/.test(password)) {
    return 'missing_uppercase'
  }
  if (!/[a-z]/.test(password)) {
    return 'missing_lowercase'
  }
  if (!/\d/.test(password)) {
    return 'missing_digit'
  }
  return null
}

/**
 * Maps a rule code to its i18n key under the shared `common.password_rules`
 * namespace. Views resolve the key with their own `t()` instance.
 */
export function passwordRuleKey(code: PasswordRuleCode): string {
  return `common.password_rules.${code}`
}
