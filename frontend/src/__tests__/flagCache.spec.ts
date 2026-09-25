import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  FLAG_CACHE_PREFIX,
  FLAG_CACHE_UNKNOWN_ORG,
  FLAG_CACHE_VERSION,
  clearFlagCache,
  flagCacheKey,
  parseFlagCache,
  serializeFlagCache,
} from '../config/flagCache'

describe('flagCache (FAR-1237 — org-scoped persisted flag map)', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  describe('flagCacheKey', () => {
    it('namespaces the key by org id', () => {
      expect(flagCacheKey('org-a')).toBe(`${FLAG_CACHE_PREFIX}:org-a`)
    })

    it('falls back to the shared unknown bucket for null/undefined/empty', () => {
      const expected = `${FLAG_CACHE_PREFIX}:${FLAG_CACHE_UNKNOWN_ORG}`
      expect(flagCacheKey(null)).toBe(expected)
      expect(flagCacheKey(undefined)).toBe(expected)
      expect(flagCacheKey('')).toBe(expected)
    })
  })

  describe('parseFlagCache', () => {
    it('returns the flat boolean map for a valid payload', () => {
      expect(parseFlagCache(serializeFlagCache({ a: true, b: false }))).toEqual({ a: true, b: false })
    })

    it('accepts an empty-but-present map', () => {
      expect(parseFlagCache(serializeFlagCache({}))).toEqual({})
    })

    it('rejects absent, corrupt, version-mismatched and non-record payloads', () => {
      expect(parseFlagCache(null)).toBeNull()
      expect(parseFlagCache('')).toBeNull()
      expect(parseFlagCache('{not json')).toBeNull()
      expect(parseFlagCache(JSON.stringify({ v: FLAG_CACHE_VERSION + 1, flags: { a: true } }))).toBeNull()
      for (const raw of ['null', '0', 'false', '5', '"off"', '[]']) {
        expect(parseFlagCache(raw)).toBeNull()
      }
    })

    it('rejects a payload whose flags field is missing, primitive or an array', () => {
      expect(parseFlagCache(JSON.stringify({ v: FLAG_CACHE_VERSION }))).toBeNull()
      expect(parseFlagCache(JSON.stringify({ v: FLAG_CACHE_VERSION, flags: 5 }))).toBeNull()
      expect(parseFlagCache(JSON.stringify({ v: FLAG_CACHE_VERSION, flags: [] }))).toBeNull()
    })

    it('rejects a payload with any non-boolean flag value', () => {
      expect(
        parseFlagCache(JSON.stringify({ v: FLAG_CACHE_VERSION, flags: { a: 'yes' } })),
      ).toBeNull()
    })
  })

  describe('serializeFlagCache', () => {
    it('emits a versioned payload', () => {
      expect(JSON.parse(serializeFlagCache({ a: true }))).toEqual({
        v: FLAG_CACHE_VERSION,
        flags: { a: true },
      })
    })

    it('rejects any non-boolean value so an unreadable cache is never written', () => {
      expect(() => serializeFlagCache({ a: 'yes' } as unknown as Record<string, boolean>)).toThrow(TypeError)
      expect(() => serializeFlagCache({ a: true, b: 1 } as unknown as Record<string, boolean>)).toThrow(TypeError)
    })
  })

  describe('clearFlagCache', () => {
    it('removes every org bucket and leaves unrelated keys untouched', () => {
      localStorage.setItem(flagCacheKey('org-a'), serializeFlagCache({ a: true }))
      localStorage.setItem(flagCacheKey(null), serializeFlagCache({ a: true }))
      localStorage.setItem('modulo_access_token', 'keep-me')

      clearFlagCache()

      expect(localStorage.getItem(flagCacheKey('org-a'))).toBeNull()
      expect(localStorage.getItem(flagCacheKey(null))).toBeNull()
      expect(localStorage.getItem('modulo_access_token')).toBe('keep-me')
    })

    it('is a no-op when localStorage is unavailable', () => {
      vi.stubGlobal('localStorage', undefined)
      expect(() => clearFlagCache()).not.toThrow()
    })

    it('skips a null key slot without throwing', () => {
      vi.stubGlobal('localStorage', {
        length: 1,
        key: () => null,
        removeItem: vi.fn(),
      } as unknown as Storage)
      expect(() => clearFlagCache()).not.toThrow()
    })

    it('swallows a storage failure', () => {
      vi.stubGlobal('localStorage', {
        get length(): number {
          throw new Error('storage blocked')
        },
      } as unknown as Storage)
      expect(() => clearFlagCache()).not.toThrow()
    })
  })
})
