import { describe, it, expect, vi, beforeEach } from 'vitest'

const mockManifest = vi.hoisted(() => ({
  sidebar_groups: {
    core: { label: 'BUILD', order: 1, default_expanded: true, labelKey: 'components.SidebarNav.group_build' },
    monitor: { label: 'MONITOR', order: 2, default_expanded: true, labelKey: 'components.SidebarNav.group_monitor' },
    configure: { label: 'CONFIGURE', order: 3, default_expanded: false, labelKey: 'components.SidebarNav.group_configure' },
    admin: { label: 'ADMIN', order: 4, default_expanded: false, labelKey: 'components.SidebarNav.group_admin' },
  },
  routes: {
    '/': { name: 'dashboard', breadcrumb: 'Dashboard', sidebar_group: 'core', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/notifications': { name: 'notifications', breadcrumb: 'Notifications', sidebar_group: null, sidebar_order: null, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/pipelines': { name: 'pipeline-list', breadcrumb: 'Pipelines', sidebar_group: 'core', sidebar_order: 3, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/library': { name: 'library', breadcrumb: 'Library', sidebar_group: 'core', sidebar_order: 4, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null },
    '/runs': { name: 'runs-list', breadcrumb: 'Runs', sidebar_group: 'core', sidebar_order: 5, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/runs/:id': { name: 'run-detail', breadcrumb: 'Run Detail', sidebar_group: 'core', sidebar_order: 8, type: 'detail_page', required_tier: null, required_roles: null, required_permissions: null },
    '/lifecycle-maps': { name: 'lifecycle-maps', breadcrumb: 'Lifecycle Maps', sidebar_group: 'core', sidebar_order: 9, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/runs/diff': { name: 'runs-diff', breadcrumb: 'Output Diff', sidebar_group: 'monitor', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/evals/editor': { name: 'eval-editor', breadcrumb: 'Evals', sidebar_group: 'monitor', sidebar_order: 2, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/evals/proposals': { name: 'eval-proposals-queue', breadcrumb: 'Eval Proposals', sidebar_group: 'monitor', sidebar_order: 3, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null },
    '/variants/compare': { name: 'variant-compare', breadcrumb: 'Variants', sidebar_group: 'monitor', sidebar_order: 4, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/variants/ab-test': { name: 'ab-test-models', breadcrumb: 'AB Test Models', sidebar_group: 'monitor', sidebar_order: 5, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/settings/observability': { name: 'settings-observability', breadcrumb: 'Observability', sidebar_group: 'monitor', sidebar_order: 6, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/monitoring': { name: 'settings-monitoring', breadcrumb: 'Browser Monitoring', sidebar_group: 'monitor', sidebar_order: 7, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/schemas': { name: 'schemas', breadcrumb: 'Schemas', sidebar_group: 'configure', sidebar_order: 1, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/schemas/editor/:id': { name: 'schema-editor', breadcrumb: 'Schema Editor', sidebar_group: 'configure', sidebar_order: 2, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/schemas/infer': { name: 'schema-infer', breadcrumb: 'Schema Inference', sidebar_group: 'configure', sidebar_order: 3, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/parameter-schemas': { name: 'admin-parameter-schemas', breadcrumb: 'Parameter Schemas', sidebar_group: 'configure', sidebar_order: 4, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/connectors': { name: 'admin-connectors', breadcrumb: 'Connectors', sidebar_group: 'configure', sidebar_order: 5, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/mcp': { name: 'settings-mcp', breadcrumb: 'MCP', sidebar_group: 'configure', sidebar_order: 6, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/settings/triggers': { name: 'settings-triggers', breadcrumb: 'Triggers', sidebar_group: 'configure', sidebar_order: 7, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/settings/runtime-config': { name: 'settings-runtime-config', breadcrumb: 'Runtime Config', sidebar_group: 'configure', sidebar_order: 11, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/rate-limits': { name: 'settings-rate-limits', breadcrumb: 'Rate Limits', sidebar_group: 'configure', sidebar_order: 9, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/model-backends': { name: 'admin-model-backends', breadcrumb: 'Model Backends', sidebar_group: 'configure', sidebar_order: 10, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/environment-profiles': { name: 'environment-profiles', breadcrumb: 'Environment Profiles', sidebar_group: 'configure', sidebar_order: 8, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null, exact: true },
    '/admin/costs': { name: 'admin-costs', breadcrumb: 'Costs', sidebar_group: 'configure', sidebar_order: 12, type: 'page', required_tier: 'team', required_roles: ['admin'], required_permissions: null, exact: true },
    '/admin/costs/limits': { name: 'admin-costs-limits', breadcrumb: 'Spend Limits', sidebar_group: null, sidebar_order: null, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/costs/controls': { name: 'admin-costs-controls', breadcrumb: 'Cost Controls', sidebar_group: null, sidebar_order: null, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/license': { name: 'settings-license', breadcrumb: 'License', sidebar_group: 'admin', sidebar_order: 1, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/teams': { name: 'settings-teams', breadcrumb: 'Teams', sidebar_group: 'admin', sidebar_order: 2, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/sso': { name: 'settings-sso', breadcrumb: 'SSO', sidebar_group: 'admin', sidebar_order: 3, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/hitl-review': { name: 'settings-hitl-review', breadcrumb: 'HITL Review', sidebar_group: 'admin', sidebar_order: 4, type: 'page', required_tier: null, required_roles: null, required_permissions: null, visibility: 'private_preview' },
    '/settings/error-forwarders': { name: 'settings-error-forwarders', breadcrumb: 'Error Forwarders', sidebar_group: 'admin', sidebar_order: 5, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/email': { name: 'settings-email', breadcrumb: 'Email', sidebar_group: 'admin', sidebar_order: 6, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/remy': { name: 'admin-remy', breadcrumb: 'Remy Config', sidebar_group: 'admin', sidebar_order: 7, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/remy': { name: 'settings-remy', breadcrumb: 'Remy Skills', sidebar_group: 'admin', sidebar_order: 8, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/users': { name: 'admin-users', breadcrumb: 'Users', sidebar_group: 'admin', sidebar_order: 9, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/org': { name: 'admin-org', breadcrumb: 'Org Settings', sidebar_group: 'admin', sidebar_order: 10, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/audit': { name: 'admin-audit', breadcrumb: 'Audit Log', sidebar_group: 'admin', sidebar_order: 11, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/housekeeping': { name: 'admin-housekeeping', breadcrumb: 'Housekeeping', sidebar_group: 'admin', sidebar_order: 13, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/feedback/inbox': { name: 'feedback-inbox', breadcrumb: 'Feedback Inbox', sidebar_group: 'admin', sidebar_order: 14, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/my-profile': { name: 'my-profile', breadcrumb: 'My Profile', sidebar_group: null, sidebar_order: null, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
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
    expect(navGroups).toHaveLength(4)
    const groupIds = navGroups.map((g) => g.id)
    expect(groupIds).toEqual(['core', 'monitor', 'configure', 'admin'])
  })

  it('sets defaultCollapsed based on default_expanded', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.defaultCollapsed).toBe(false)

    const configure = navGroups.find((g) => g.id === 'configure')!
    expect(configure.defaultCollapsed).toBe(true)

    const admin = navGroups.find((g) => g.id === 'admin')!
    expect(admin.defaultCollapsed).toBe(true)
  })

  it('groups are sorted by manifest order', () => {
    expect(navGroups[0].id).toBe('core')
    expect(navGroups[1].id).toBe('monitor')
    expect(navGroups[2].id).toBe('configure')
    expect(navGroups[3].id).toBe('admin')
  })

  it('items within groups are sorted by sidebar_order', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.items).toHaveLength(5)
    expect(core.items[0].to).toBe('/')
    expect(core.items.map(i => i.to)).toEqual(['/', '/pipelines', '/library', '/runs', '/lifecycle-maps'])
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

  it('includes lifecycle-maps in the core group', () => {
    const allItems = navGroups.flatMap((g) => g.items)
    const lifecycle = allItems.find((item) => item.to === '/lifecycle-maps')
    expect(lifecycle).toBeDefined()
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.items.find((item) => item.to === '/lifecycle-maps')).toBeDefined()
  })

  it('sets requiredRoles and requiredTier on items', () => {
    const admin = navGroups.find((g) => g.id === 'admin')!
    const teams = admin.items.find((item) => item.to === '/settings/teams')!
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
      requiredTier: 'team',
    }
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: (t) => t === 'team' })).toBe(true)
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: (t) => t !== 'team' })).toBe(false)
  })

  it('canSeeItem checks both role and tier', () => {
    const item: NavItem = {
      to: '/super-admin',
      icon: 'Shield',
      label: 'Super Admin',
      labelKey: 'item_super_admin',
      requiredRoles: ['admin'],
      requiredTier: 'team',
    }
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: (t) => t === 'team' })).toBe(true)
    expect(canSeeItem(item, { role: 'viewer' }, { isAtMinimumTier: (t) => t === 'team' })).toBe(false)
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: (t) => t !== 'team' })).toBe(false)
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

  it('canSeeItem denies access when requiredRoles is an empty array (empty whitelist)', () => {
    const item: NavItem = {
      to: '/admin',
      icon: 'Settings',
      label: 'Admin',
      labelKey: 'item_admin',
      requiredRoles: [],
    }
    expect(canSeeItem(item, { role: 'admin' }, { isAtMinimumTier: () => true })).toBe(false)
    expect(canSeeItem(item, { role: 'viewer' }, { isAtMinimumTier: () => true })).toBe(false)
  })

  it('canSeeItem denies access when requiredPermissions is an empty array (empty whitelist)', () => {
    const item: NavItem = {
      to: '/admin',
      icon: 'Settings',
      label: 'Admin',
      labelKey: 'item_admin',
      requiredPermissions: [],
    }
    expect(canSeeItem(item, { role: 'admin', permissions: ['admin.read'] }, { isAtMinimumTier: () => true })).toBe(false)
  })

  it('canSeeItem treats null and undefined restrictions as no restriction', () => {
    const item: NavItem = {
      to: '/free',
      icon: 'File',
      label: 'Free',
      labelKey: 'item_free',
      requiredRoles: null,
      requiredTier: null,
      requiredPermissions: null,
    }
    expect(canSeeItem(item, { role: 'viewer' }, { isAtMinimumTier: () => false })).toBe(true)
  })

  it('sets labelKey from routeLabelKeyMap for known routes', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    const dash = core.items.find((item) => item.to === '/')!
    expect(dash.labelKey).toBe('components.SidebarNav.item_dashboard')
  })

  it('sets group labelKey from manifest labelKey', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.labelKey).toBe('components.SidebarNav.group_build')

    const monitor = navGroups.find((g) => g.id === 'monitor')!
    expect(monitor.labelKey).toBe('components.SidebarNav.group_monitor')
  })

  it('configure group is collapsed by default (not expanded)', () => {
    const configure = navGroups.find((g) => g.id === 'configure')!
    expect(configure.defaultCollapsed).toBe(true)
  })

  it('admin group is collapsed by default', () => {
    const admin = navGroups.find((g) => g.id === 'admin')!
    expect(admin.defaultCollapsed).toBe(true)
  })

  it('core has exactly 5 items (lifecycle-maps restored, connectors moved, stages removed)', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.items).toHaveLength(5)
  })

  it('monitor has evals, diff, observability, etc', () => {
    const monitor = navGroups.find((g) => g.id === 'monitor')!
    expect(monitor.items.length).toBeGreaterThanOrEqual(3)
    expect(monitor.items.some(i => i.to === '/runs/diff')).toBe(true)
    expect(monitor.items.some(i => i.to === '/evals/editor')).toBe(true)
  })

  it('configure group contains connectors, schemas, costs etc', () => {
    const configure = navGroups.find((g) => g.id === 'configure')!
    expect(configure.items.length).toBeGreaterThanOrEqual(5)
    expect(configure.items.some(i => i.to === '/admin/connectors')).toBe(true)
    expect(configure.items.some(i => i.to === '/admin/costs')).toBe(true)
  })

  it('admin group contains license, users, remy, system etc', () => {
    const admin = navGroups.find((g) => g.id === 'admin')!
    expect(admin.items.length).toBeGreaterThanOrEqual(5)
    expect(admin.items.some(i => i.to === '/settings/license')).toBe(true)
    expect(admin.items.some(i => i.to === '/admin/users')).toBe(true)
    expect(admin.items.some(i => i.to === '/admin/remy')).toBe(true)
  })
})
