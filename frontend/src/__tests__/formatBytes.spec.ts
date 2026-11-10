import { describe, it, expect } from 'vitest'
import { formatBytes } from '../lib/formatBytes'

describe('formatBytes', () => {
  it('renders absent, non-finite, or negative input as an em-dash', () => {
    expect(formatBytes(null)).toBe('—')
    expect(formatBytes(undefined)).toBe('—')
    expect(formatBytes(Number.NaN)).toBe('—')
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe('—')
    expect(formatBytes(-1)).toBe('—')
  })

  it('honours a caller-supplied invalid label', () => {
    expect(formatBytes(null, { invalidLabel: 'n/a' })).toBe('n/a')
  })

  it('renders zero using the localized zero label', () => {
    expect(formatBytes(0)).toBe('0 B')
  })

  it('keeps byte values below 1 KiB in bytes', () => {
    expect(formatBytes(1)).toBe('1 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(999)).toBe('999 B')
  })

  it('scales through the binary units', () => {
    expect(formatBytes(1024)).toBe('1 KB')
    expect(formatBytes(1024 ** 2)).toBe('1 MB')
    expect(formatBytes(1024 ** 3)).toBe('1 GB')
    expect(formatBytes(1024 ** 4)).toBe('1 TB')
  })

  it('shows at most one fraction digit and trims trailing zeros', () => {
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 ** 2)).toBe('5 MB')
    expect(formatBytes(50 * 1024 ** 2)).toBe('50 MB')
    expect(formatBytes(8 * 1024 ** 3)).toBe('8 GB')
    expect(formatBytes(1.34 * 1024 ** 2)).toBe('1.3 MB')
  })

  it('clamps values beyond terrabytes to the largest unit', () => {
    expect(formatBytes(1024 ** 6)).toMatch(/ TB$/)
  })
})
