/**
 * Log display transforms for RunDetailView (FAR-849).
 *
 * Pure functions — testable in isolation.
 */

/**
 * Pretty-print a raw log string:
 * - Unescapes literal `\n`, `\t`, `\r` sequences (common in JSON-encoded output)
 * - If the result is valid JSON, pretty-prints it with 2-space indent
 * - Returns the rendered string and whether any transformation was applied
 */
export function prettyPrintLog(raw: string): { html: string; applied: boolean } {
  if (!raw) return { html: '', applied: false }
  let s = raw
  // Unescape literal backslash-n / backslash-t that appear in JSON-encoded output
  const hadEscapes = /\\[nrt]/.test(s)
  if (hadEscapes) {
    s = s.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\r/g, '\r')
  }
  // Try to parse as JSON and pretty-print
  let jsonFormatted = false
  try {
    const parsed = JSON.parse(s)
    s = JSON.stringify(parsed, null, 2)
    jsonFormatted = true
  } catch {
    // Not JSON — leave as-is (with escapes resolved)
  }
  const applied = hadEscapes || jsonFormatted
  return { html: s, applied }
}

/**
 * ANSI escape sequence pattern — matches SGR codes like `\x1B[31m`.
 */
const ANSI_RE = /\x1B\[[0-9;]*[A-Za-z]/g

/**
 * Strip ANSI escape sequences from a string so raw codes are never shown.
 */
export function stripAnsi(raw: string): string {
  return raw.replace(ANSI_RE, '')
}

/**
 * Check whether a string contains ANSI escape sequences.
 */
export function hasAnsiSequences(raw: string): boolean {
  // Reset lastIndex since we use a global regex
  ANSI_RE.lastIndex = 0
  return ANSI_RE.test(raw)
}
