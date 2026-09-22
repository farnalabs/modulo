import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useLoginPrefs } from '../composables/useLoginPrefs'

describe('useLoginPrefs', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('returns null when no preference is stored', () => {
    const prefs = useLoginPrefs()
    expect(prefs.getLastOrgSlug()).toBeNull()
    expect(prefs.getLastMethod()).toBeNull()
  })

  it('round-trips the org slug and login method', () => {
    const prefs = useLoginPrefs()
    prefs.setLastOrgSlug('acme')
    prefs.setLastMethod('okta')
    expect(prefs.getLastOrgSlug()).toBe('acme')
    expect(prefs.getLastMethod()).toBe('okta')
  })

  it('trims stored values on both write and read', () => {
    const prefs = useLoginPrefs()
    prefs.setLastOrgSlug('  acme  ')
    expect(localStorage.getItem('modulo_login_last_org')).toBe('acme')
    localStorage.setItem('modulo_login_last_method', '  okta  ')
    expect(prefs.getLastMethod()).toBe('okta')
  })

  it('returns null for a stored blank org slug', () => {
    localStorage.setItem('modulo_login_last_org', '   ')
    expect(useLoginPrefs().getLastOrgSlug()).toBeNull()
  })

  it('returns null for a stored blank login method', () => {
    localStorage.setItem('modulo_login_last_method', '   ')
    expect(useLoginPrefs().getLastMethod()).toBeNull()
  })

  it('ignores a blank org slug on write', () => {
    useLoginPrefs().setLastOrgSlug('   ')
    expect(localStorage.getItem('modulo_login_last_org')).toBeNull()
  })

  it('ignores a blank login method on write', () => {
    useLoginPrefs().setLastMethod('')
    expect(localStorage.getItem('modulo_login_last_method')).toBeNull()
  })

  it('clears the stored org slug', () => {
    const prefs = useLoginPrefs()
    prefs.setLastOrgSlug('acme')
    prefs.clearLastOrgSlug()
    expect(prefs.getLastOrgSlug()).toBeNull()
  })

  it('degrades silently when storage reads throw', () => {
    // jsdom's localStorage Proxy ignores instance-level reassignment, so the
    // throwing stub is installed on Storage.prototype.
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    const prefs = useLoginPrefs()
    expect(prefs.getLastOrgSlug()).toBeNull()
    expect(prefs.getLastMethod()).toBeNull()
  })

  it('degrades silently when storage writes throw', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded')
    })
    const prefs = useLoginPrefs()
    expect(() => prefs.setLastOrgSlug('acme')).not.toThrow()
    expect(() => prefs.setLastMethod('okta')).not.toThrow()
  })

  it('degrades silently when storage removal throws', () => {
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    const prefs = useLoginPrefs()
    expect(() => prefs.clearLastOrgSlug()).not.toThrow()
  })
})
