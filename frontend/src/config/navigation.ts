export interface NavItem {
  to: string
  icon: string
  label: string
  exact?: boolean
}

export interface NavGroup {
  id: string
  label: string
  items: NavItem[]
  defaultCollapsed: boolean
  simpleMode: boolean
  systemAdminOnly?: boolean
}

export const navGroups: NavGroup[] = [
  {
    id: 'core',
    label: 'Core',
    simpleMode: true,
    defaultCollapsed: false,
    items: [
      { to: '/', icon: 'LayoutDashboard', label: 'Dashboard' },
      { to: '/notifications', icon: 'Bell', label: 'Notifications' },
    ],
  },
  {
    id: 'pipelines',
    label: 'Pipelines',
    simpleMode: true,
    defaultCollapsed: false,
    items: [
      { to: '/library', icon: 'BookOpen', label: 'Library' },
      { to: '/templates', icon: 'LayoutTemplate', label: 'Templates' },
      { to: '/pipelines/copy', icon: 'Copy', label: 'Copy Pipeline' },
      { to: '/stages', icon: 'Columns', label: 'Stages Board' },
    ],
  },
  {
    id: 'runs-evaluation',
    label: 'Runs & Evaluation',
    simpleMode: true,
    defaultCollapsed: true,
    items: [
      { to: '/runs/diff', icon: 'GitCommit', label: 'Output Diff' },
      { to: '/evals/editor', icon: 'CheckSquare', label: 'Evals' },
      { to: '/evals/proposals', icon: 'Clipboard', label: 'Eval Proposals' },
      { to: '/variants/compare', icon: 'GitFork', label: 'Variants' },
      { to: '/variants/ab-test', icon: 'FlaskConical', label: 'AB Test Models' },
    ],
  },
  {
    id: 'schemas',
    label: 'Schemas',
    simpleMode: true,
    defaultCollapsed: true,
    items: [
      { to: '/schemas', icon: 'Database', label: 'Browse', exact: true },
      { to: '/schemas/editor', icon: 'Database', label: 'Editor' },
      { to: '/schemas/infer', icon: 'Database', label: 'Infer' },
    ],
  },
  {
    id: 'remy',
    label: 'Remy',
    simpleMode: true,
    defaultCollapsed: true,
    items: [
      { to: '/settings/remy', icon: 'Bot', label: 'My Skills' },
      { to: '/admin/remy', icon: 'Settings', label: 'Admin Config' },
    ],
  },
  {
    id: 'settings',
    label: 'Settings',
    simpleMode: true,
    defaultCollapsed: true,
    items: [
      { to: '/settings/teams', icon: 'Users', label: 'Teams' },
      { to: '/settings/sso', icon: 'Shield', label: 'SSO' },
      { to: '/settings/license', icon: 'KeyRound', label: 'License' },
      { to: '/settings/mcp', icon: 'Cable', label: 'MCP' },
      { to: '/settings/triggers', icon: 'Zap', label: 'Triggers' },
      { to: '/settings/runtime-config', icon: 'Settings', label: 'Runtime Config' },
      { to: '/settings/rate-limits', icon: 'Gauge', label: 'Rate Limits' },
      { to: '/settings/hitl-review', icon: 'ShieldQuestion', label: 'HITL Review' },
      { to: '/settings/observability', icon: 'Eye', label: 'Observability' },
      { to: '/settings/error-forwarders', icon: 'AlertTriangle', label: 'Error Forwarders' },
    ],
  },
  {
    id: 'admin-access-control',
    label: 'Access Control',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/users', icon: 'UserCircle', label: 'Users' },
      { to: '/admin/org', icon: 'Building', label: 'Org Settings' },
      { to: '/admin/audit', icon: 'FileText', label: 'Audit Log' },
    ],
  },
  {
    id: 'admin-cost-management',
    label: 'Cost Management',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/costs', icon: 'DollarSign', label: 'Overview' },
      { to: '/admin/costs/limits', icon: 'CreditCard', label: 'Spend Limits' },
      { to: '/admin/costs/controls', icon: 'SlidersHorizontal', label: 'Cost Controls' },
    ],
  },
  {
    id: 'admin-system',
    label: 'System',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/connectors', icon: 'Plug', label: 'Connectors' },
      { to: '/admin/model-backends', icon: 'Cpu', label: 'Model Backends' },
      { to: '/admin/node-categories', icon: 'Tag', label: 'Node Categories' },
      { to: '/admin/feature-flags', icon: 'Flag', label: 'Feature Flags' },
      { to: '/admin/environments', icon: 'Container', label: 'Environments' },
      { to: '/admin/run-retention', icon: 'Clock', label: 'Run Retention' },
      { to: '/admin/views', icon: 'Eye', label: 'Saved Views' },
    ],
  },
  {
    id: 'admin-monitoring',
    label: 'Monitoring',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/errors', icon: 'AlertTriangle', label: 'Error Dashboard' },
      { to: '/admin/notification-delivery', icon: 'Bell', label: 'Notification Log' },
      { to: '/admin/api-changelog', icon: 'History', label: 'API Changelog' },
      { to: '/admin/teams/comparison', icon: 'BarChart', label: 'Team Comparison' },
    ],
  },
  {
    id: 'admin-extensions',
    label: 'Extensions',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/plugins', icon: 'Puzzle', label: 'Plugins' },
      { to: '/feedback/inbox', icon: 'MessageSquare', label: 'Feedback Inbox' },
    ],
  },
  {
    id: 'system',
    label: 'System',
    simpleMode: false,
    defaultCollapsed: true,
    systemAdminOnly: true,
    items: [
      { to: '/admin/system/orgs', icon: 'Building2', label: 'Organisations' },
      { to: '/admin/system/config', icon: 'Settings', label: 'System Config' },
    ],
  },
]
