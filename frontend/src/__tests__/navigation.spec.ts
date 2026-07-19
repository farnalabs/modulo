import { describe, it, expect, vi, beforeEach } from 'vitest'

const mockManifest = vi.hoisted(() => ({
  sidebar_groups: {
    core: {
      label: 'Core',
      order: 1,
      default_expanded: true,
      simple_mode: true,
      labelKey: 'components.SidebarNav.group_core',
    },
    remy: {
      label: 'Remy',
      order: 2,
      default_expanded: false,
      simple_mode: true,
      labelKey: 'components.SidebarNav.group_remy',
    },
    settings: {
      label: 'Settings',
      order: 3,
      default_expanded: true,
      simple_mode: true,
      labelKey: 'components.SidebarNav.group_settings',
    },
    'access-control': {
      label: 'Access Control',
      order: 4,
      default_expanded: true,
      simple_mode: false,
      labelKey: 'components.SidebarNav.group_access_control',
    },
    monitoring: {
      label: 'Monitoring',
      order: 5,
      default_expanded: true,
      simple_mode: false,
      labelKey: 'components.SidebarNav.group_monitoring',
    },
  },
  routes: {
    '/': {
      name: 'dashboard',
      breadcrumb: 'Dashboard',
      sidebar_group: 'core',
      sidebar_order: 1,
      type: 'page',
      required_tier: null,
      required_roles: null,
      exact: true,
    },
    '/notifications': {
      name: 'notifications',
      breadcrumb: 'Notifications',
      sidebar_group: 'core',
      sidebar_order: 2,
      type: 'page',
      required_tier: null,
      required_roles: null,
    },
    '/settings/teams': {
      name: 'settings-teams',
      breadcrumb: 'Teams',
      sidebar_group: 'settings',
      sidebar_order: 1,
      type: 'list_page',
      required_tier: 'team',
      required_roles: ['admin'],
    },
    '/settings/license': {
      name: 'settings-license',
      breadcrumb: 'License',
      sidebar_group: 'settings',
      sidebar_order: 2,
      type: 'form_page',
      required_tier: 'team',
      required_roles: ['admin'],
    },
    '/admin/users': {
      name: 'admin-users',
      breadcrumb: 'Users',
      sidebar_group: 'access-control',
      sidebar_order: 1,
      type: 'list_page',
      required_tier: 'team',
      required_roles: ['admin'],
    },
    '/admin/audit': {
      name: 'admin-audit',
      breadcrumb: 'Audit Log',
      sidebar_group: 'access-control',
      sidebar_order: 2,
      type: 'list_page',
      required_tier: 'team',
      required_roles: ['admin'],
    },
    '/runs/:id': {
      name: 'run-detail',
      breadcrumb: 'Run Detail',
      sidebar_group: 'core',
      sidebar_order: 99,
      type: 'detail_page',
      required_tier: null,
      required_roles: null,
    },
    '/admin/my-profile': {
      name: 'my-profile',
      breadcrumb: 'My Profile',
      sidebar_group: null,
      sidebar_order: null,
      type: 'page',
      required_tier: null,
      required_roles: null,
    },
    '/admin/monitoring': {
      name: 'admin-monitoring',
      breadcrumb: 'Monitoring Dashboard',
      sidebar_group: 'monitoring',
      sidebar_order: 1,
      type: 'page',
      required_tier: 'team',
      required_roles: ['admin'],
    },
    '/settings/remy': {
      name: 'settings-remy',
      breadcrumb: 'My Skills',
      sidebar_group: 'remy',
      sidebar_order: 1,
      type: 'page',
      required_tier: null,
      required_roles: null,
    },
  },
}))

vi.mock('@/manifest.yaml', () => ({
  default: mockManifest,
}))

import { getNavGroups, canSeeItem } from '../config/navigation'
const navGroups = getNavGroups()
import type { NavItem } from '../config/navigation'

describe('navigation.ts', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('populates sidebar groups from manifest', () => {
    expect(navGroups.length).toBe(5)
    const groupIds = navGroups.map((g) => g.id)
    expect(groupIds).toEqual(['core', 'remy', 'settings', 'access-control', 'monitoring'])
  })

  it('sets defaultCollapsed based on default_expanded', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.defaultCollapsed).toBe(false)

    const remy = navGroups.find((g) => g.id === 'remy')!
    expect(remy.defaultCollapsed).toBe(true)
  })

  it('sets simpleMode from manifest', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.simpleMode).toBe(true)

    const ac = navGroups.find((g) => g.id === 'access-control')!
    expect(ac.simpleMode).toBe(false)
  })

  it('groups are sorted by manifest order', () => {
    expect(navGroups[0].id).toBe('core')
    expect(navGroups[1].id).toBe('remy')
    expect(navGroups[2].id).toBe('settings')
  })

  it('items within groups are sorted by sidebar_order', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.items.length).toBe(2)
    expect(core.items[0].to).toBe('/')
    expect(core.items[1].to).toBe('/notifications')
  })

  it('excludes detail_page items from sidebar', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    const runDetail = core.items.find((item) => item.to === '/runs/:id')
    expect(runDetail).toBeUndefined()
  })

  it('excludes routes without sidebar_group', () => {
    const allItems = navGroups.flatMap((g) => g.items)
    const myProfile = allItems.find((item) => item.to === '/admin/my-profile')
    expect(myProfile).toBeUndefined()
  })

  it('sets requiredRoles and requiredTier on items', () => {
    const settings = navGroups.find((g) => g.id === 'settings')!
    const teams = settings.items.find((item) => item.to === '/settings/teams')!
    expect(teams.requiredRoles).toEqual(['admin'])
    expect(teams.requiredTier).toBe('team')
  })

  it('canSeeItem returns true when no restrictions', () => {
    const item: NavItem = {
      to: '/',
      icon: 'LayoutDashboard',
      label: 'Dashboard',
      labelKey: 'item_dashboard',
    }
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: () => true })).toBe(true)
    expect(canSeeItem(item, { role: 'viewer' }, { isAtMinimumTier: () => true })).toBe(true)
  })

  it('canSeeItem filters by role', () => {
    const item: NavItem = {
      to: '/admin',
      icon: 'Settings',
      label: 'Admin',
      labelKey: 'item_admin',
      requiredRoles: ['admin'],
    }
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: () => true })).toBe(true)
    expect(canSeeItem(item, { role: 'viewer' }, { isAtMinimumTier: () => true })).toBe(false)
  })

  it('canSeeItem filters by tier', () => {
    const item: NavItem = {
      to: '/enterprise',
      icon: 'Star',
      label: 'Enterprise',
      labelKey: 'item_enterprise',
      requiredTier: 'enterprise',
    }
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: (t) => t === 'enterprise' })).toBe(true)
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: (t) => t !== 'enterprise' })).toBe(false)
  })

  it('canSeeItem checks both role and tier', () => {
    const item: NavItem = {
      to: '/super-admin',
      icon: 'Shield',
      label: 'Super Admin',
      labelKey: 'item_super_admin',
      requiredRoles: ['admin'],
      requiredTier: 'enterprise',
    }
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: (t) => t === 'enterprise' })).toBe(true)
    expect(canSeeItem(item, { role: 'viewer' }, { isAtMinimumTier: (t) => t === 'enterprise' })).toBe(false)
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: (t) => t !== 'enterprise' })).toBe(false)
  })

  it('exports canSeeItem with correct type signature', () => {
    const item: NavItem = {
      to: '/test',
      icon: 'File',
      label: 'Test',
      labelKey: 'item_test',
      requiredTier: null,
    }
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: () => true })).toBe(true)
  })

  it('canSeeItem filters by permissions', () => {
    const item: NavItem = {
      to: '/admin',
      icon: 'Settings',
      label: 'Admin',
      labelKey: 'item_admin',
      requiredPermissions: ['admin.read', 'admin.write'],
    }
    expect(canSeeItem(item, { role: 'admin', permissions: ['admin.read'] }, { isAtMinimumTier: () => true })).toBe(true)
    expect(canSeeItem(item, { role: 'admin', permissions: ['user.read'] }, { isAtMinimumTier: () => true })).toBe(false)
    expect(canSeeItem(item, { role: 'admin', permissions: ['admin.read', 'user.read'] }, { isAtMinimumTier: () => true })).toBe(true)
  })

  it('canSeeItem denies access when permissions not provided but required', () => {
    const item: NavItem = {
      to: '/admin',
      icon: 'Settings',
      label: 'Admin',
      labelKey: 'item_admin',
      requiredPermissions: ['admin.read'],
    }
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: () => true })).toBe(false)
  })

  it('canSeeItem denies access when permissions array is empty', () => {
    const item: NavItem = {
      to: '/admin',
      icon: 'Settings',
      label: 'Admin',
      labelKey: 'item_admin',
      requiredPermissions: ['admin.read'],
    }
    expect(canSeeItem(item, { role: 'admin', permissions: [] }, { isAtMinimumTier: () => true })).toBe(false)
  })

  it('sets labelKey from routeLabelKeyMap for known routes', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    const dash = core.items.find((item) => item.to === '/')!
    expect(dash.labelKey).toBe('components.SidebarNav.item_dashboard')
  })

  it('sets group labelKey from manifest labelKey', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.labelKey).toBe('components.SidebarNav.group_core')
  })
})
