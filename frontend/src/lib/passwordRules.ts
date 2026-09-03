export type PasswordRuleCode = 'too_short' | 'missing_uppercase' | 'missing_lowercase' | 'missing_digit'

export const PASSWORD_MIN_LENGTH = 8

/**
 * Client-side password UX pre-check (FAR-461). This is NOT a mirror of the
 * backend contract: the backend (`validate_password_strength` in
 * `auth/passwords.py`) requires length ≥ 8, ≤ 72 UTF-8 bytes, and ≥ 30 bits
 * of Shannon entropy — with NO character-class rules. These client rules are
 * intentionally stricter than the backend (requiring upper + lower + digit
 * classes implies ≳ 47 bits), so a password accepted here is guaranteed to
 * pass the server. The two rule sets evolve independently: this is a
 * sign-up-UX nudge only, and the backend remains the authoritative gate.
 *
 * Returns the FIRST failing rule as a stable error-code string, or null when
 * the password satisfies every client rule.
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
