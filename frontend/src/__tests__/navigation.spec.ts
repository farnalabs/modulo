import { describe, it, expect, vi, beforeEach } from 'vitest'

const mockManifest = vi.hoisted(() => ({
  sidebar_groups: {
    lifecycle: { label: 'Lifecycle', order: 0, default_expanded: true, simple_mode: true, labelKey: 'components.SidebarNav.group_lifecycle' },
    core: { label: 'Core', order: 1, default_expanded: true, simple_mode: true, labelKey: 'components.SidebarNav.group_core' },
    analysis: { label: 'Analysis', order: 2, default_expanded: true, simple_mode: false, labelKey: 'components.SidebarNav.group_analysis' },
    evals: { label: 'Evals', order: 3, default_expanded: true, simple_mode: false, labelKey: 'components.SidebarNav.group_evals' },
    schemas: { label: 'Schemas', order: 4, default_expanded: true, simple_mode: false, labelKey: 'components.SidebarNav.group_schemas' },
    remy: { label: 'Remy', order: 5, default_expanded: true, simple_mode: true, labelKey: 'components.SidebarNav.group_remy' },
    settings: { label: 'Settings', order: 6, default_expanded: true, simple_mode: true, labelKey: 'components.SidebarNav.group_settings' },
    'access-control': { label: 'Access Control', order: 7, default_expanded: true, simple_mode: false, labelKey: 'components.SidebarNav.group_access_control' },
    'cost-management': { label: 'Cost Management', order: 8, default_expanded: true, simple_mode: false, labelKey: 'components.SidebarNav.group_cost_management' },
    system: { label: 'System', order: 9, default_expanded: true, simple_mode: false, labelKey: 'components.SidebarNav.group_system' },
    monitoring: { label: 'Monitoring', order: 10, default_expanded: true, simple_mode: false, labelKey: 'components.SidebarNav.group_monitoring' },
    extensions: { label: 'Extensions', order: 11, default_expanded: true, simple_mode: false, labelKey: 'components.SidebarNav.group_extensions' },
  },
  routes: {
    '/': { name: 'dashboard', breadcrumb: 'Dashboard', sidebar_group: 'core', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/notifications': { name: 'notifications', breadcrumb: 'Notifications', sidebar_group: null, sidebar_order: null, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/pipelines': { name: 'pipeline-list', breadcrumb: 'Pipelines', sidebar_group: 'core', sidebar_order: 3, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/library': { name: 'library', breadcrumb: 'Library', sidebar_group: 'core', sidebar_order: 4, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null },
    '/runs': { name: 'runs-list', breadcrumb: 'Runs', sidebar_group: 'core', sidebar_order: 5, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/admin/connectors': { name: 'admin-connectors', breadcrumb: 'Connectors', sidebar_group: 'core', sidebar_order: 6, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/stages': { name: 'stages', breadcrumb: 'Stages Board', sidebar_group: 'core', sidebar_order: 7, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/runs/:id': { name: 'run-detail', breadcrumb: 'Run Detail', sidebar_group: 'core', sidebar_order: 8, type: 'detail_page', required_tier: null, required_roles: null, required_permissions: null },
    '/lifecycle-maps': { name: 'lifecycle-maps', breadcrumb: 'Lifecycle Maps', sidebar_group: 'core', sidebar_order: 9, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/runs/diff': { name: 'runs-diff', breadcrumb: 'Output Diff', sidebar_group: 'analysis', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/evals/editor': { name: 'eval-editor', breadcrumb: 'Evals', sidebar_group: 'evals', sidebar_order: 1, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/evals/proposals': { name: 'eval-proposals-queue', breadcrumb: 'Eval Proposals', sidebar_group: 'evals', sidebar_order: 2, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, visibility: 'private_preview' },
    '/variants/compare': { name: 'variant-compare', breadcrumb: 'Variants', sidebar_group: 'evals', sidebar_order: 3, type: 'page', required_tier: null, required_roles: null, required_permissions: null, visibility: 'private_preview' },
    '/variants/ab-test': { name: 'ab-test-models', breadcrumb: 'AB Test Models', sidebar_group: 'evals', sidebar_order: 4, type: 'page', required_tier: null, required_roles: null, required_permissions: null, visibility: 'private_preview' },
    '/schemas': { name: 'schemas', breadcrumb: 'Schemas', sidebar_group: 'schemas', sidebar_order: 1, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/schemas/editor/:id': { name: 'schema-editor', breadcrumb: 'Schema Editor', sidebar_group: 'schemas', sidebar_order: 2, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/schemas/infer': { name: 'schema-infer', breadcrumb: 'Schema Inference', sidebar_group: 'schemas', sidebar_order: 3, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/parameter-schemas': { name: 'admin-parameter-schemas', breadcrumb: 'Parameter Schemas', sidebar_group: 'schemas', sidebar_order: 4, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/remy': { name: 'admin-remy', breadcrumb: 'Remy Config', sidebar_group: 'remy', sidebar_order: 1, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/remy': { name: 'settings-remy', breadcrumb: 'Remy Skills', sidebar_group: 'remy', sidebar_order: 2, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/settings/teams': { name: 'settings-teams', breadcrumb: 'Teams', sidebar_group: 'settings', sidebar_order: 1, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/sso': { name: 'settings-sso', breadcrumb: 'SSO', sidebar_group: 'settings', sidebar_order: 2, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/license': { name: 'settings-license', breadcrumb: 'License', sidebar_group: 'settings', sidebar_order: 3, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/mcp': { name: 'settings-mcp', breadcrumb: 'MCP', sidebar_group: 'settings', sidebar_order: 4, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/settings/triggers': { name: 'settings-triggers', breadcrumb: 'Triggers', sidebar_group: 'settings', sidebar_order: 5, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/settings/runtime-config': { name: 'settings-runtime-config', breadcrumb: 'Runtime Config', sidebar_group: 'settings', sidebar_order: 7, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/rate-limits': { name: 'settings-rate-limits', breadcrumb: 'Rate Limits', sidebar_group: 'settings', sidebar_order: 8, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/hitl-review': { name: 'settings-hitl-review', breadcrumb: 'HITL Review', sidebar_group: 'settings', sidebar_order: 9, type: 'page', required_tier: null, required_roles: null, required_permissions: null, visibility: 'private_preview' },
    '/settings/observability': { name: 'settings-observability', breadcrumb: 'Observability', sidebar_group: 'settings', sidebar_order: 10, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/error-forwarders': { name: 'settings-error-forwarders', breadcrumb: 'Error Forwarders', sidebar_group: 'settings', sidebar_order: 11, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/settings/email': { name: 'settings-email', breadcrumb: 'Email', sidebar_group: 'settings', sidebar_order: 12, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/users': { name: 'admin-users', breadcrumb: 'Users', sidebar_group: 'access-control', sidebar_order: 1, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/org': { name: 'admin-org', breadcrumb: 'Org Settings', sidebar_group: 'access-control', sidebar_order: 2, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/audit': { name: 'admin-audit', breadcrumb: 'Audit Log', sidebar_group: 'access-control', sidebar_order: 3, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/costs': { name: 'admin-costs', breadcrumb: 'Cost Overview', sidebar_group: 'cost-management', sidebar_order: 1, type: 'page', required_tier: 'team', required_roles: ['admin'], required_permissions: null, exact: true },
    '/admin/costs/limits': { name: 'admin-costs-limits', breadcrumb: 'Spend Limits', sidebar_group: 'cost-management', sidebar_order: 2, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/costs/controls': { name: 'admin-costs-controls', breadcrumb: 'Cost Controls', sidebar_group: 'cost-management', sidebar_order: 3, type: 'form_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/admin/model-backends': { name: 'admin-model-backends', breadcrumb: 'Model Backends', sidebar_group: 'system', sidebar_order: 2, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/environment-profiles': { name: 'environment-profiles', breadcrumb: 'Environment Profiles', sidebar_group: 'system', sidebar_order: 8, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null, exact: true },
    '/settings/monitoring': { name: 'settings-monitoring', breadcrumb: 'Browser Monitoring', sidebar_group: 'monitoring', sidebar_order: 6, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/housekeeping': { name: 'admin-housekeeping', breadcrumb: 'Housekeeping', sidebar_group: 'monitoring', sidebar_order: 3, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/feedback/inbox': { name: 'feedback-inbox', breadcrumb: 'Feedback Inbox', sidebar_group: 'extensions', sidebar_order: 2, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null },
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
    expect(navGroups.length).toBe(11)
    const groupIds = navGroups.map((g) => g.id)
    expect(groupIds).toEqual(['core', 'analysis', 'evals', 'schemas', 'remy', 'settings', 'access-control', 'cost-management', 'system', 'monitoring', 'extensions'])
  })

  it('sets defaultCollapsed based on default_expanded', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.defaultCollapsed).toBe(false)

    const remy = navGroups.find((g) => g.id === 'remy')!
    expect(remy.defaultCollapsed).toBe(false)
  })

  it('sets simpleMode from manifest', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.simpleMode).toBe(true)

    const ac = navGroups.find((g) => g.id === 'access-control')!
    expect(ac.simpleMode).toBe(false)
  })

  it('groups are sorted by manifest order', () => {
    expect(navGroups[0].id).toBe('core')
    expect(navGroups[1].id).toBe('analysis')
    expect(navGroups[2].id).toBe('evals')
  })

  it('items within groups are sorted by sidebar_order', () => {
    const core = navGroups.find((g) => g.id === 'core')!
    expect(core.items.length).toBe(7)
    expect(core.items[0].to).toBe('/')
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

