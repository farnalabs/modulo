import { describe, it, expect, vi, afterEach } from 'vitest'
import { randomId } from '../../lib/randomId'

// FAR-2070 — dedicated coverage for the shared random-ID helper. It replaces
// four near-identical local copies (assistant tabs, UI command executor,
// product analytics, lifecycle-map editor) plus the auth tab id, so its whole
// precedence chain is pinned here once instead of indirectly at each call site.

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('randomId', () => {
  it('uses crypto.randomUUID when available', () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'uuid-value' })
    expect(randomId()).toBe('uuid-value')
  })

  it('prefixes the generated id when a prefix is given', () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'uuid-value' })
    expect(randomId('tab')).toBe('tab-uuid-value')
  })

  it('falls back to CSPRNG bytes when randomUUID is unavailable', () => {
    vi.stubGlobal('crypto', {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(0xab)
        return bytes
      },
    })
    expect(randomId()).toBe('ab'.repeat(16))
    expect(randomId('tab')).toBe(`tab-${'ab'.repeat(16)}`)
  })

  it('falls back to a timestamp/random id only when no CSPRNG exists', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5)
    vi.stubGlobal('crypto', undefined)
    expect(randomId()).toMatch(/^[a-z0-9]+-i$/)
    expect(randomId('tab')).toMatch(/^tab-[a-z0-9]+-i$/)
  })
})
