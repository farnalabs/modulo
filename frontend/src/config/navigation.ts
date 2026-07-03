export interface NavItem {
  to: string
  icon: string
  label: string
  labelKey: string
  exact?: boolean
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

export const navGroups: NavGroup[] = [
  {
    id: 'core',
    label: 'Core',
    labelKey: 'components.SidebarNav.group_core',
    simpleMode: true,
    defaultCollapsed: false,
    items: [
      { to: '/', icon: 'LayoutDashboard', label: 'Dashboard', labelKey: 'components.SidebarNav.item_dashboard' },
      { to: '/notifications', icon: 'Bell', label: 'Notifications', labelKey: 'components.SidebarNav.item_notifications' },
    ],
  },
  {
    id: 'pipelines',
    label: 'Pipelines',
    labelKey: 'components.SidebarNav.group_pipelines',
    simpleMode: true,
    defaultCollapsed: false,
    items: [
      { to: '/library', icon: 'BookOpen', label: 'Library', labelKey: 'components.SidebarNav.item_library' },
      { to: '/templates', icon: 'LayoutTemplate', label: 'Templates', labelKey: 'components.SidebarNav.item_templates' },
      { to: '/pipelines/copy', icon: 'Copy', label: 'Copy Pipeline', labelKey: 'components.SidebarNav.item_copy_pipeline' },
      { to: '/stages', icon: 'Columns', label: 'Stages Board', labelKey: 'components.SidebarNav.item_stages_board' },
    ],
  },
  {
    id: 'runs-evaluation',
    label: 'Runs & Evaluation',
    labelKey: 'components.SidebarNav.group_runs_evaluation',
    simpleMode: true,
    defaultCollapsed: true,
    items: [
      { to: '/runs/diff', icon: 'GitCommit', label: 'Output Diff', labelKey: 'components.SidebarNav.item_output_diff' },
      { to: '/evals/editor', icon: 'CheckSquare', label: 'Evals', labelKey: 'components.SidebarNav.item_evals' },
      { to: '/evals/proposals', icon: 'Clipboard', label: 'Eval Proposals', labelKey: 'components.SidebarNav.item_eval_proposals' },
      { to: '/variants/compare', icon: 'GitFork', label: 'Variants', labelKey: 'components.SidebarNav.item_variants' },
      { to: '/variants/ab-test', icon: 'FlaskConical', label: 'AB Test Models', labelKey: 'components.SidebarNav.item_ab_test_models' },
    ],
  },
  {
    id: 'schemas',
    label: 'Schemas',
    labelKey: 'components.SidebarNav.group_schemas',
    simpleMode: true,
    defaultCollapsed: true,
    items: [
      { to: '/schemas', icon: 'Database', label: 'Browse', labelKey: 'components.SidebarNav.item_browse', exact: true },
      { to: '/schemas/editor', icon: 'Database', label: 'Editor', labelKey: 'components.SidebarNav.item_editor' },
      { to: '/schemas/infer', icon: 'Database', label: 'Infer', labelKey: 'components.SidebarNav.item_infer' },
    ],
  },
  {
    id: 'remy',
    label: 'Remy',
    labelKey: 'components.SidebarNav.group_remy',
    simpleMode: true,
    defaultCollapsed: true,
    items: [
      { to: '/settings/remy', icon: 'Bot', label: 'My Skills', labelKey: 'components.SidebarNav.item_my_skills' },
      { to: '/admin/remy', icon: 'Settings', label: 'Admin Config', labelKey: 'components.SidebarNav.item_admin_config' },
    ],
  },
  {
    id: 'settings',
    label: 'Settings',
    labelKey: 'components.SidebarNav.group_settings',
    simpleMode: true,
    defaultCollapsed: true,
    items: [
      { to: '/settings/teams', icon: 'Users', label: 'Teams', labelKey: 'components.SidebarNav.item_teams' },
      { to: '/settings/sso', icon: 'Shield', label: 'SSO', labelKey: 'components.SidebarNav.item_sso' },
      { to: '/settings/license', icon: 'KeyRound', label: 'License', labelKey: 'components.SidebarNav.item_license' },
      { to: '/settings/mcp', icon: 'Cable', label: 'MCP', labelKey: 'components.SidebarNav.item_mcp' },
      { to: '/settings/triggers', icon: 'Zap', label: 'Triggers', labelKey: 'components.SidebarNav.item_triggers' },
      { to: '/settings/runtime-config', icon: 'Settings', label: 'Runtime Config', labelKey: 'components.SidebarNav.item_runtime_config' },
      { to: '/settings/rate-limits', icon: 'Gauge', label: 'Rate Limits', labelKey: 'components.SidebarNav.item_rate_limits' },
      { to: '/settings/hitl-review', icon: 'ShieldQuestion', label: 'HITL Review', labelKey: 'components.SidebarNav.item_hitl_review' },
      { to: '/settings/observability', icon: 'Eye', label: 'Observability', labelKey: 'components.SidebarNav.item_observability' },
      { to: '/settings/error-forwarders', icon: 'AlertTriangle', label: 'Error Forwarders', labelKey: 'components.SidebarNav.item_error_forwarders' },
    ],
  },
  {
    id: 'admin-access-control',
    label: 'Access Control',
    labelKey: 'components.SidebarNav.group_access_control',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/users', icon: 'UserCircle', label: 'Users', labelKey: 'components.SidebarNav.item_users' },
      { to: '/admin/org', icon: 'Building', label: 'Org Settings', labelKey: 'components.SidebarNav.item_org_settings' },
      { to: '/admin/audit', icon: 'FileText', label: 'Audit Log', labelKey: 'components.SidebarNav.item_audit_log' },
    ],
  },
  {
    id: 'admin-cost-management',
    label: 'Cost Management',
    labelKey: 'components.SidebarNav.group_cost_management',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/costs', icon: 'DollarSign', label: 'Overview', labelKey: 'components.SidebarNav.item_overview', exact: true },
      { to: '/admin/costs/limits', icon: 'CreditCard', label: 'Spend Limits', labelKey: 'components.SidebarNav.item_spend_limits' },
      { to: '/admin/costs/controls', icon: 'SlidersHorizontal', label: 'Cost Controls', labelKey: 'components.SidebarNav.item_cost_controls' },
    ],
  },
  {
    id: 'admin-system',
    label: 'System',
    labelKey: 'components.SidebarNav.group_system',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/connectors', icon: 'Plug', label: 'Connectors', labelKey: 'components.SidebarNav.item_connectors' },
      { to: '/admin/model-backends', icon: 'Cpu', label: 'Model Backends', labelKey: 'components.SidebarNav.item_model_backends' },
      { to: '/admin/node-categories', icon: 'Tag', label: 'Node Categories', labelKey: 'components.SidebarNav.item_node_categories' },
      { to: '/admin/feature-flags', icon: 'Flag', label: 'Feature Flags', labelKey: 'components.SidebarNav.item_feature_flags' },
      { to: '/admin/environments', icon: 'Container', label: 'Environments', labelKey: 'components.SidebarNav.item_environments' },
      { to: '/admin/run-retention', icon: 'Clock', label: 'Run Retention', labelKey: 'components.SidebarNav.item_run_retention' },
      { to: '/admin/views', icon: 'Eye', label: 'Saved Views', labelKey: 'components.SidebarNav.item_saved_views' },
    ],
  },
  {
    id: 'admin-monitoring',
    label: 'Monitoring',
    labelKey: 'components.SidebarNav.group_monitoring',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/errors', icon: 'AlertTriangle', label: 'Error Dashboard', labelKey: 'components.SidebarNav.item_error_dashboard' },
      { to: '/admin/notification-delivery', icon: 'Bell', label: 'Notification Log', labelKey: 'components.SidebarNav.item_notification_log' },
      { to: '/admin/api-changelog', icon: 'History', label: 'API Changelog', labelKey: 'components.SidebarNav.item_api_changelog' },
      { to: '/admin/teams/comparison', icon: 'BarChart', label: 'Team Comparison', labelKey: 'components.SidebarNav.item_team_comparison' },
    ],
  },
  {
    id: 'admin-extensions',
    label: 'Extensions',
    labelKey: 'components.SidebarNav.group_extensions',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/plugins', icon: 'Puzzle', label: 'Plugins', labelKey: 'components.SidebarNav.item_plugins' },
      { to: '/feedback/inbox', icon: 'MessageSquare', label: 'Feedback Inbox', labelKey: 'components.SidebarNav.item_feedback_inbox' },
    ],
  },
  {
    id: 'system',
    label: 'System',
    labelKey: 'components.SidebarNav.group_system_admin',
    simpleMode: false,
    defaultCollapsed: true,
    systemAdminOnly: true,
    items: [
      { to: '/admin/system/orgs', icon: 'Building2', label: 'Organisations', labelKey: 'components.SidebarNav.item_organisations' },
      { to: '/admin/system/config', icon: 'Settings', label: 'System Config', labelKey: 'components.SidebarNav.item_system_config' },
    ],
  },
]
