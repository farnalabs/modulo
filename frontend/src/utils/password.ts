// FAR-460: client-side password generator for the admin create-user form.
// Meets the form's displayed complexity rules: 8+ characters with at least
// one uppercase letter, one lowercase letter, and one digit.

const LOWER = 'abcdefghijklmnopqrstuvwxyz'
const UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
const DIGITS = '0123456789'
// Single no-look-alike alphabet for filler characters.
const FILLER = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789' + '!@#$%^&*'

function pick(chars: string): string {
  const buf = new Uint32Array(1)
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(buf)
  } else {
    buf[0] = Math.floor(Math.random() * 0xffffffff)
  }
  return chars[buf[0] % chars.length]
}

export function generateStrongPassword(length = 16): string {
  if (length < 8) length = 8

  // Guarantee one of each required class, then fill to length.
  const result: string[] = [pick(LOWER), pick(UPPER), pick(DIGITS)]
  while (result.length < length) result.push(pick(FILLER))

  // Fisher-Yates shuffle so the guaranteed classes aren't always first.
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[result[i], result[j]] = [result[j], result[i]]
  }
  return result.join('')
}
