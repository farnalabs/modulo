import manifest from '@/manifest.yaml'

export interface NavItem {
  to: string
  icon: string
  label: string
  labelKey: string
  exact?: boolean
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
  simpleMode: boolean
  systemAdminOnly?: boolean
}

const routeIconMap: Record<string, string> = {
  dashboard: 'LayoutDashboard',
  notifications: 'Bell',
  'pipeline-list': 'BookOpen',
  library: 'BookOpen',
  'pipeline-templates': 'LayoutTemplate',
  'pipeline-copy': 'Copy',
  stages: 'Columns',
  'runs-diff': 'GitCommit',
  'eval-editor': 'CheckSquare',
  'eval-proposals-queue': 'Clipboard',
  'variant-compare': 'GitFork',
  'ab-test-models': 'FlaskConical',
  schemas: 'Database',
  'schema-editor': 'FileText',
  'schema-infer': 'Search',
  'settings-remy': 'Bot',
  'admin-remy': 'Settings',
  'settings-teams': 'Users',
  'settings-sso': 'Shield',
  'settings-license': 'KeyRound',
  'settings-mcp': 'Cable',
  'settings-triggers': 'Zap',
  'admin-triggers': 'Zap',
  'settings-runtime-config': 'Settings',
  'settings-rate-limits': 'Gauge',
  'settings-hitl-review': 'ShieldQuestion',
  'settings-observability': 'Eye',
  'settings-error-forwarders': 'AlertTriangle',
  'admin-users': 'UserCircle',
  'admin-org': 'Building',
  'admin-audit': 'FileText',
  'admin-costs': 'DollarSign',
  'admin-costs-limits': 'CreditCard',
  'admin-costs-controls': 'SlidersHorizontal',
  'admin-connectors': 'Plug',
  'admin-model-backends': 'Cpu',
  'admin-node-categories': 'Tag',
  'admin-feature-flags': 'Flag',
  'admin-environments': 'Container',
  'admin-run-retention': 'Clock',
  'admin-views': 'Eye',
  'admin-pipelines': 'BookOpen',
  'admin-errors': 'AlertTriangle',
  'admin-error-detail': 'AlertTriangle',
  'admin-notification-delivery': 'Bell',
  'api-changelog': 'History',
  'team-comparison': 'BarChart',
  'admin-plugins': 'Puzzle',
  'feedback-inbox': 'MessageSquare',
}

const routeLabelKeyMap: Record<string, string> = {
  dashboard: 'components.SidebarNav.item_dashboard',
  notifications: 'components.SidebarNav.item_notifications',
  library: 'components.SidebarNav.item_library',
  'pipeline-list': 'components.SidebarNav.item_my_pipelines',
  'pipeline-templates': 'components.SidebarNav.item_templates',
  'pipeline-copy': 'components.SidebarNav.item_copy_pipeline',
  stages: 'components.SidebarNav.item_stages_board',
  'runs-diff': 'components.SidebarNav.item_output_diff',
  'eval-editor': 'components.SidebarNav.item_evals',
  'eval-proposals-queue': 'components.SidebarNav.item_eval_proposals',
  'variant-compare': 'components.SidebarNav.item_variants',
  'ab-test-models': 'components.SidebarNav.item_ab_test_models',
  schemas: 'components.SidebarNav.item_browse',
  'schema-editor': 'components.SidebarNav.item_editor',
  'schema-infer': 'components.SidebarNav.item_infer',
  'settings-remy': 'components.SidebarNav.item_my_skills',
  'admin-remy': 'components.SidebarNav.item_admin_config',
  'settings-teams': 'components.SidebarNav.item_teams',
  'settings-sso': 'components.SidebarNav.item_sso',
  'settings-license': 'components.SidebarNav.item_license',
  'settings-mcp': 'components.SidebarNav.item_mcp',
  'admin-triggers': 'components.SidebarNav.item_triggers',
  'settings-triggers': 'components.SidebarNav.item_triggers',
  'settings-runtime-config': 'components.SidebarNav.item_runtime_config',
  'settings-rate-limits': 'components.SidebarNav.item_rate_limits',
  'settings-monitoring': 'components.SidebarNav.item_browser_monitoring',
  'settings-hitl-review': 'components.SidebarNav.item_hitl_review',
  'settings-observability': 'components.SidebarNav.item_observability',
  'settings-error-forwarders': 'components.SidebarNav.item_error_forwarders',
  'admin-users': 'components.SidebarNav.item_users',
  'admin-org': 'components.SidebarNav.item_org_settings',
  'admin-audit': 'components.SidebarNav.item_audit_log',
  'admin-costs': 'components.SidebarNav.item_overview',
  'admin-costs-limits': 'components.SidebarNav.item_spend_limits',
  'admin-costs-controls': 'components.SidebarNav.item_cost_controls',
  'admin-connectors': 'components.SidebarNav.item_connectors',
  'admin-model-backends': 'components.SidebarNav.item_model_backends',
  'admin-node-categories': 'components.SidebarNav.item_node_categories',
  'admin-feature-flags': 'components.SidebarNav.item_feature_flags',
  'admin-environments': 'components.SidebarNav.item_environments',
  'admin-run-retention': 'components.SidebarNav.item_run_retention',
  'admin-pipelines': 'components.SidebarNav.item_admin_pipelines',
  'admin-views': 'components.SidebarNav.item_saved_views',
  'admin-errors': 'components.SidebarNav.item_error_dashboard',
  'admin-notification-delivery': 'components.SidebarNav.item_notification_log',
  'api-changelog': 'components.SidebarNav.item_api_changelog',
  'team-comparison': 'components.SidebarNav.item_team_comparison',
  'admin-plugins': 'components.SidebarNav.item_plugins',
  'feedback-inbox': 'components.SidebarNav.item_feedback_inbox',
}

const groupLabelKeyMap: Record<string, string> = {
  core: 'components.SidebarNav.group_core',
  remy: 'components.SidebarNav.group_remy',
  settings: 'components.SidebarNav.group_settings',
  'access-control': 'components.SidebarNav.group_access_control',
  'cost-management': 'components.SidebarNav.group_cost_management',
  system: 'components.SidebarNav.group_system',
  monitoring: 'components.SidebarNav.group_monitoring',
  extensions: 'components.SidebarNav.group_extensions',
  'system-admin': 'components.SidebarNav.group_system_admin',
}

interface ManifestRoute {
  name: string
  breadcrumb: string
  sidebar_group?: string | null
  sidebar_order?: number | null
  type?: string
  exact?: boolean
  required_tier?: string | null
  required_roles?: string[] | null
  required_permissions?: string[] | null
}

interface ManifestSidebarGroup {
  label: string
  order: number
  default_expanded: boolean
  simple_mode: boolean
  labelKey?: string
}

interface Manifest {
  routes: Record<string, ManifestRoute>
  sidebar_groups: Record<string, ManifestSidebarGroup>
}

function isManifestRoute(route: ManifestRoute): route is ManifestRoute & { sidebar_group: string; sidebar_order: number } {
  return typeof route.sidebar_group === 'string' && typeof route.sidebar_order === 'number'
}

function buildSidebarGroups(): NavGroup[] {
  const m = manifest as Manifest
  const orderMap: Record<string, number> = {}
  const itemsByGroup: Record<string, NavItem[]> = {}

  for (const routeGroup of Object.values(m.sidebar_groups || {})) {
    orderMap[routeGroup.label] = 0
  }

  for (const [path, route] of Object.entries(m.routes || {})) {
    if (!route.sidebar_group) continue
    if (route.type === 'detail_page') continue
    if (!isManifestRoute(route)) continue

    const group = m.sidebar_groups[route.sidebar_group]
    if (!group) continue

    if (!itemsByGroup[route.sidebar_group]) {
      itemsByGroup[route.sidebar_group] = []
    }

    itemsByGroup[route.sidebar_group].push({
      to: path,
      icon: routeIconMap[route.name] || 'File',
      label: route.breadcrumb,
      labelKey: routeLabelKeyMap[route.name] || `nav.${route.name}`,
      exact: route.exact || undefined,
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

  const groups = Object.entries(m.sidebar_groups || {})
    .sort(([, a], [, b]) => a.order - b.order)
    .map(([id, sg]) => {
      const items = itemsByGroup[id] || []
      return {
        id,
        label: sg.label,
        labelKey: sg.labelKey || groupLabelKeyMap[id] || `components.SidebarNav.group_${id}`,
        items,
        defaultCollapsed: !sg.default_expanded,
        simpleMode: sg.simple_mode,
      }
    })

  return groups
}

export function canSeeItem(
  item: NavItem,
  user: { role: string },
  plan: { isAtMinimumTier: (tier: string) => boolean },
): boolean {
  if (item.requiredRoles && item.requiredRoles.length > 0 && !item.requiredRoles.includes(user.role)) return false
  if (item.requiredTier && !plan.isAtMinimumTier(item.requiredTier)) return false
  return true
}

export const navGroups: NavGroup[] = buildSidebarGroups()
