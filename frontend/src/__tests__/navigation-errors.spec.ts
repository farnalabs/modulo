import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

describe('navigation.ts error handling', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  describe('malformed manifest', () => {
    beforeEach(() => {
      vi.resetModules()
      vi.doMock('@/manifest.yaml', () => ({ default: null }))
    })

    it('returns empty groups and logs an error when the manifest is not valid', async () => {
      const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
      const { getNavGroups } = await import('../config/navigation')
      expect(getNavGroups()).toEqual([])
      expect(errorSpy).toHaveBeenCalled()
    })
  })

  describe('route referencing a non-existent sidebar_group', () => {
    beforeEach(() => {
      vi.resetModules()
      vi.doMock('@/manifest.yaml', () => ({
        default: {
          sidebar_groups: {
            core: { label: 'BUILD', order: 1, default_expanded: true },
          },
          routes: {
            '/': { name: 'dashboard', breadcrumb: 'Dashboard', sidebar_group: 'core', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
            '/orphan': { name: 'orphan', breadcrumb: 'Orphan', sidebar_group: 'ghost', sidebar_order: 2, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
          },
        },
      }))
    })

    it('logs a console.warn and skips the orphaned route', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
      const { getNavGroups } = await import('../config/navigation')
      const groups = getNavGroups()
      expect(warnSpy).toHaveBeenCalled()
      expect(String(warnSpy.mock.calls[0][0])).toContain('ghost')
      expect(groups).toHaveLength(1)
      expect(groups[0].items.map((i) => i.to)).toEqual(['/'])
    })
  })

  describe('route missing sidebar_order', () => {
    beforeEach(() => {
      vi.resetModules()
      vi.doMock('@/manifest.yaml', () => ({
        default: {
          sidebar_groups: {
            core: { label: 'BUILD', order: 1, default_expanded: true },
          },
          routes: {
            '/': { name: 'dashboard', breadcrumb: 'Dashboard', sidebar_group: 'core', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
            '/no-order': { name: 'no-order', breadcrumb: 'No Order', sidebar_group: 'core', type: 'page', required_tier: null, required_roles: null, required_permissions: null },
          },
        },
      }))
    })

    it('skips routes without a numeric sidebar_order', async () => {
      const { getNavGroups } = await import('../config/navigation')
      const groups = getNavGroups()
      expect(groups[0].items.map((i) => i.to)).toEqual(['/'])
    })
  })
})
