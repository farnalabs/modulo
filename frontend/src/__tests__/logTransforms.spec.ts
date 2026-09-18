import { describe, it, expect } from 'vitest'
import { prettyPrintLog, stripAnsi, hasAnsiSequences } from '../utils/logTransforms'

describe('prettyPrintLog', () => {
  it('returns empty for empty input', () => {
    const result = prettyPrintLog('')
    expect(result.html).toBe('')
    expect(result.applied).toBe(false)
  })

  it('unescapes literal \\n sequences', () => {
    const input = 'line1\\nline2\\nline3'
    const result = prettyPrintLog(input)
    expect(result.html).toBe('line1\nline2\nline3')
    expect(result.applied).toBe(true)
  })

  it('unescapes literal \\t sequences', () => {
    const input = 'col1\\tcol2\\tcol3'
    const result = prettyPrintLog(input)
    expect(result.html).toBe('col1\tcol2\tcol3')
    expect(result.applied).toBe(true)
  })

  it('unescapes literal \\r sequences', () => {
    const input = 'before\\rafter'
    const result = prettyPrintLog(input)
    expect(result.html).toBe('before\rafter')
    expect(result.applied).toBe(true)
  })

  it('pretty-prints valid JSON', () => {
    const input = '{"key":"value","nested":{"a":1}}'
    const result = prettyPrintLog(input)
    expect(result.applied).toBe(true)
    expect(result.html).toContain('"key": "value"')
    expect(result.html).toContain('"nested":')
    // Should be indented
    expect(result.html).toContain('\n')
  })

  it('unescapes AND pretty-prints JSON with escaped newlines', () => {
    // Common pattern: JSON-encoded log output with literal \n
    const input = '{"output":"Collecting pip-audit\\n  Obtaining depende\\u2026"}'
    const result = prettyPrintLog(input)
    expect(result.applied).toBe(true)
    expect(result.html).toContain('"output":')
    // The \n should be unescaped inside the JSON value
    expect(result.html).toContain('Collecting pip-audit\n  Obtaining depende')
  })

  it('does not break non-JSON content', () => {
    const input = 'Just a plain log line with no escapes'
    const result = prettyPrintLog(input)
    expect(result.html).toBe(input)
    expect(result.applied).toBe(false)
  })

  it('handles malformed JSON gracefully', () => {
    const input = '{not valid json\\nwith escapes}'
    const result = prettyPrintLog(input)
    // Escapes are unescaped but JSON is not formatted (not valid)
    expect(result.html).toBe('{not valid json\nwith escapes}')
    expect(result.applied).toBe(true)
  })
})

describe('stripAnsi', () => {
  it('removes ANSI colour codes', () => {
    const input = '\x1B[31mred text\x1B[0m'
    expect(stripAnsi(input)).toBe('red text')
  })

  it('removes multiple ANSI codes', () => {
    const input = '\x1B[1m\x1B[32mbold green\x1B[0m normal'
    expect(stripAnsi(input)).toBe('bold green normal')
  })

  it('leaves strings without ANSI codes unchanged', () => {
    const input = 'plain text with no codes'
    expect(stripAnsi(input)).toBe(input)
  })

  it('handles empty string', () => {
    expect(stripAnsi('')).toBe('')
  })
})

describe('hasAnsiSequences', () => {
  it('detects ANSI escape sequences', () => {
    expect(hasAnsiSequences('\x1B[31mred\x1B[0m')).toBe(true)
  })

  it('returns false for plain text', () => {
    expect(hasAnsiSequences('no codes here')).toBe(false)
  })

  it('returns false for empty string', () => {
    expect(hasAnsiSequences('')).toBe(false)
  })
})
