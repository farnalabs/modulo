import manifest from '@/manifest.yaml'

export interface NavItem {
  to: string
  icon: string
  label: string
  labelKey: string
  exact?: boolean
  visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
  requiredRoles?: string[] | null
  requiredTier?: string | null
  requiredPermissions?: string[] | null
}

export interface NavGroup {
  id: string
  label: string
  labelKey: string
  items: NavItem[]
  defaultCollapsed: boolean
  systemAdminOnly?: boolean
}

const routeConfigMap: Record<string, { icon: string; labelKey: string }> = {
  dashboard: { icon: 'LayoutDashboard', labelKey: 'components.SidebarNav.item_dashboard' },
  analytics: { icon: 'BarChart', labelKey: 'components.SidebarNav.item_analytics' },
  notifications: { icon: 'Bell', labelKey: 'components.SidebarNav.item_notifications' },
  library: { icon: 'BookOpen', labelKey: 'components.SidebarNav.item_library' },
  'lifecycle-maps': { icon: 'Map', labelKey: 'components.SidebarNav.item_lifecycle_maps' },
  'pipeline-list': { icon: 'GitFork', labelKey: 'components.SidebarNav.item_my_pipelines' },

  'pipeline-copy': { icon: 'Copy', labelKey: 'components.SidebarNav.item_copy_pipeline' },
  'runs-list': { icon: 'CirclePlay', labelKey: 'components.SidebarNav.item_runs_list' },
  'runs-diff': { icon: 'GitCommit', labelKey: 'components.SidebarNav.item_output_diff' },
  'eval-editor': { icon: 'CheckSquare', labelKey: 'components.SidebarNav.item_evals' },
  'eval-proposals-queue': { icon: 'Clipboard', labelKey: 'components.SidebarNav.item_eval_proposals' },
  'variant-compare': { icon: 'GitFork', labelKey: 'components.SidebarNav.item_variants' },
  'ab-test-models': { icon: 'FlaskConical', labelKey: 'components.SidebarNav.item_ab_test_models' },
  schemas: { icon: 'Database', labelKey: 'components.SidebarNav.item_schemas' },
  'schema-editor': { icon: 'FileText', labelKey: 'components.SidebarNav.item_editor' },
  'schema-infer': { icon: 'Search', labelKey: 'components.SidebarNav.item_infer' },
  'settings-remy': { icon: 'Bot', labelKey: 'components.SidebarNav.item_my_skills' },
  'admin-remy': { icon: 'Settings', labelKey: 'components.SidebarNav.item_admin_config' },
  'settings-teams': { icon: 'Users', labelKey: 'components.SidebarNav.item_teams' },
  'settings-sso': { icon: 'Shield', labelKey: 'components.SidebarNav.item_sso' },
  'settings-license': { icon: 'KeyRound', labelKey: 'components.SidebarNav.item_license' },
  'settings-mcp': { icon: 'Cable', labelKey: 'components.SidebarNav.item_mcp' },
  'settings-triggers': { icon: 'Zap', labelKey: 'components.SidebarNav.item_triggers' },
  'settings-runtime-config': { icon: 'Settings', labelKey: 'components.SidebarNav.item_runtime_config' },
  'settings-rate-limits': { icon: 'Gauge', labelKey: 'components.SidebarNav.item_rate_limits' },
  'settings-monitoring': { icon: 'Eye', labelKey: 'components.SidebarNav.item_browser_monitoring' },
  'settings-hitl-review': { icon: 'ShieldQuestion', labelKey: 'components.SidebarNav.item_hitl_review' },
  'settings-observability': { icon: 'Eye', labelKey: 'components.SidebarNav.item_observability' },
  'settings-email': { icon: 'Mail', labelKey: 'components.SidebarNav.item_email_settings' },
  'settings-error-forwarders': { icon: 'AlertTriangle', labelKey: 'components.SidebarNav.item_error_forwarders' },
  'settings-guardrails': { icon: 'ShieldAlert', labelKey: 'components.SidebarNav.item_settings_guardrails' },
  'admin-users': { icon: 'UserCircle', labelKey: 'components.SidebarNav.item_users' },
  'admin-org': { icon: 'Building', labelKey: 'components.SidebarNav.item_org_settings' },
  'admin-audit': { icon: 'FileText', labelKey: 'components.SidebarNav.item_audit_log' },
  'admin-costs': { icon: 'DollarSign', labelKey: 'components.SidebarNav.item_costs' },
  'admin-costs-limits': { icon: 'CreditCard', labelKey: 'components.SidebarNav.item_spend_limits' },
  'admin-costs-controls': { icon: 'SlidersHorizontal', labelKey: 'components.SidebarNav.item_cost_controls' },
  'admin-connectors': { icon: 'Plug', labelKey: 'components.SidebarNav.item_connectors' },
  'admin-model-backends': { icon: 'Cpu', labelKey: 'components.SidebarNav.item_model_backends' },
  'admin-node-categories': { icon: 'Tag', labelKey: 'components.SidebarNav.item_node_categories' },
  'admin-feature-flags': { icon: 'Flag', labelKey: 'components.SidebarNav.item_feature_flags' },
  'admin-run-retention': { icon: 'Clock', labelKey: 'components.SidebarNav.item_run_retention' },
  'admin-sandbox-concurrency': { icon: 'Gauge', labelKey: 'components.SidebarNav.item_sandbox_concurrency' },
  'admin-views': { icon: 'Eye', labelKey: 'components.SidebarNav.item_saved_views' },
  'admin-errors': { icon: 'AlertTriangle', labelKey: 'components.SidebarNav.item_error_dashboard' },
  'admin-error-detail': { icon: 'AlertTriangle', labelKey: 'components.SidebarNav.item_error_dashboard' },
  'admin-notification-delivery': { icon: 'Bell', labelKey: 'components.SidebarNav.item_notification_log' },
  'admin-plugins': { icon: 'Puzzle', labelKey: 'components.SidebarNav.item_plugins' },
  'environment-profiles': { icon: 'Container', labelKey: 'components.SidebarNav.item_environment_profiles' },
  'feedback-inbox': { icon: 'MessageSquare', labelKey: 'components.SidebarNav.item_feedback_inbox' },
  'admin-housekeeping': { icon: 'Broom', labelKey: 'components.SidebarNav.item_housekeeping' },
  'admin-parameter-schemas': { icon: 'FileText', labelKey: 'components.SidebarNav.item_parameter_schemas' },
}

const groupLabelKeyMap: Record<string, string> = {
  build: 'components.SidebarNav.group_build',
  monitor: 'components.SidebarNav.group_monitor',
  configure: 'components.SidebarNav.group_configure',
  admin: 'components.SidebarNav.group_admin',
  improve: 'components.SidebarNav.group_improve',
  system: 'components.SidebarNav.group_system',
}

interface ManifestRoute {
  name: string
  breadcrumb: string
  sidebar_group?: string | null
  sidebar_order?: number | null
  type?: string
  exact?: boolean
  visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
  required_tier?: string | null
  required_roles?: string[] | null
  required_permissions?: string[] | null
}

interface ManifestSidebarGroup {
  label: string
  order: number
  default_expanded: boolean
  labelKey?: string
  system_admin_only?: boolean
}

interface Manifest {
  routes: Record<string, ManifestRoute>
  sidebar_groups: Record<string, ManifestSidebarGroup>
}

function isManifestRoute(
  route: ManifestRoute,
): route is ManifestRoute & { sidebar_group: string; sidebar_order: number } {
  return typeof route.sidebar_group === 'string' && typeof route.sidebar_order === 'number'
}

function isManifest(obj: unknown): obj is Manifest {
  if (typeof obj !== 'object' || obj === null) return false
  const m = obj as Record<string, unknown>
  return (
    typeof m.routes === 'object' && m.routes !== null
    && typeof m.sidebar_groups === 'object' && m.sidebar_groups !== null
  )
}

function buildSidebarGroups(): NavGroup[] {
  if (!isManifest(manifest)) {
    console.error('[navigation] manifest is invalid — missing routes or sidebar_groups')
    return []
  }

  const m: Manifest = manifest
  const itemsByGroup: Record<string, NavItem[]> = {}

  for (const [path, route] of Object.entries(m.routes)) {
    if (!route.sidebar_group) continue
    if (route.type === 'detail_page') continue
    if (!isManifestRoute(route)) continue

    const group = m.sidebar_groups[route.sidebar_group]
    if (!group) {
      console.warn(`[navigation] Route "${path}" references non-existent sidebar_group "${route.sidebar_group}"`)
      continue
    }

    (itemsByGroup[route.sidebar_group] ??= []).push({
      to: path,
      icon: routeConfigMap[route.name]?.icon || 'File',
      label: route.breadcrumb || route.name,
      labelKey: routeConfigMap[route.name]?.labelKey || `nav.${route.name}`,
      exact: route.exact || undefined,
      visibility: (route.visibility as 'public_preview' | 'private_preview' | 'in_dev') || undefined,
      requiredRoles: route.required_roles || null,
      requiredTier: route.required_tier || null,
      requiredPermissions: route.required_permissions || null,
    })
  }

  for (const groupId of Object.keys(itemsByGroup)) {
    itemsByGroup[groupId].sort((a, b) => {
      const routeA = m.routes[a.to]
      const routeB = m.routes[b.to]
      return (routeA?.sidebar_order ?? 0) - (routeB?.sidebar_order ?? 0)
    })
  }

  return Object.entries(m.sidebar_groups)
    .sort(([, a], [, b]) => a.order - b.order)
    .map(([id, sg]) => ({
      id,
      label: sg.label,
      labelKey: sg.labelKey || groupLabelKeyMap[id] || `components.SidebarNav.group_${id}`,
      items: itemsByGroup[id] || [],
      defaultCollapsed: !sg.default_expanded,
      systemAdminOnly: sg.system_admin_only || undefined,
    }))
    .filter((g) => g.items.length > 0)
}

export function canSeeItem(
  item: NavItem,
  user: { role: string; permissions?: string[] },
  plan: { isAtMinimumTier: (tier: string) => boolean },
): boolean {
  if (item.requiredRoles != null) {
    if (item.requiredRoles.length === 0 || !item.requiredRoles.includes(user.role)) return false
  }
  if (item.requiredTier && !plan.isAtMinimumTier(item.requiredTier)) return false
  if (item.requiredPermissions != null) {
    if (item.requiredPermissions.length === 0) return false
    const permissions = user.permissions
    if (!permissions || !item.requiredPermissions.some((p) => permissions.includes(p))) return false
  }
  return true
}

export interface NavVisibilityContext {
  isSystemAdmin: boolean
  userRole: string | null
  userPermissions: string[]
  devMode: boolean
  tierInfoLoaded: boolean
  isAtMinimumTier: (tier: string) => boolean
}

export function isNavItemVisible(item: NavItem, ctx: NavVisibilityContext): boolean {
  if ((item.visibility === 'private_preview' || item.visibility === 'in_dev') && !ctx.devMode) return false
  if (!item.requiredRoles && !item.requiredTier && !item.requiredPermissions) return true
  if (item.requiredTier && !ctx.tierInfoLoaded) return false
  return canSeeItem(
    item,
    { role: ctx.userRole || '', permissions: ctx.userPermissions },
    { isAtMinimumTier: ctx.isAtMinimumTier },
  )
}

export function getVisibleNavGroups(ctx: NavVisibilityContext): NavGroup[] {
  return getNavGroups()
    .filter((g) => !g.systemAdminOnly || ctx.isSystemAdmin)
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => isNavItemVisible(item, ctx)),
    }))
    .filter((g) => g.items.length > 0)
}

let _cachedGroups: NavGroup[] | null = null

export function getNavGroups(): NavGroup[] {
  if (_cachedGroups === null) {
    _cachedGroups = buildSidebarGroups()
  }
  return _cachedGroups.map((g) => ({ ...g, items: [...g.items] }))
}
