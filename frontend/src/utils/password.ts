// FAR-460: client-side password generator for the admin create-user form.
// Meets the form's displayed complexity rules: 8+ characters with at least
// one uppercase letter, one lowercase letter, and one digit.
//
// These are real handed-out credentials, so all randomness comes from
// crypto.getRandomValues — there is deliberately no Math.random fallback
// (every supported browser exposes WebCrypto) and index selection uses
// rejection sampling to avoid modulo bias.

const LOWER = 'abcdefghijklmnopqrstuvwxyz'
const UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
const DIGITS = '0123456789'
// Single no-look-alike alphabet for filler characters.
const FILLER = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789' + '!@#$%^&*'

// Uniform integer in [0, maxExclusive): rejection-sample a full UInt32 draw
// until it falls below the largest multiple of maxExclusive in range. Expected
// <2 iterations because callers pass length >= 8.
function randomInt(maxExclusive: number): number {
  const limit = Math.floor(0x100000000 / maxExclusive) * maxExclusive
  const buf = new Uint32Array(1)
  let value: number
  do {
    crypto.getRandomValues(buf)
    value = buf[0]
  } while (value >= limit)
  return value % maxExclusive
}

function pick(chars: string): string {
  return chars[randomInt(chars.length)]
}

export function generateStrongPassword(length = 16): string {
  if (length < 8) length = 8

  // Guarantee one of each required class, then fill to length.
  const result: string[] = [pick(LOWER), pick(UPPER), pick(DIGITS)]
  while (result.length < length) result.push(pick(FILLER))

  // Fisher-Yates shuffle with crypto-seeded indices so the guaranteed classes
  // aren't always first.
  for (let i = result.length - 1; i > 0; i--) {
    const j = randomInt(i + 1)
    ;[result[i], result[j]] = [result[j], result[i]]
  }
  return result.join('')
}
