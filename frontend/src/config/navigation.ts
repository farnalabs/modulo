export interface NavItem {
  to: string
  icon: string
  label: string
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
      { to: '/library', icon: 'BookOpen', label: 'Library' },
    ],
  },
  {
    id: 'pipelines',
    label: 'Pipelines',
    simpleMode: true,
    defaultCollapsed: false,
    items: [
      { to: '/pipelines', icon: 'GitBranch', label: 'Pipelines' },
      { to: '/pipelines/copy', icon: 'Copy', label: 'Copy Pipeline' },
      { to: '/templates', icon: 'LayoutTemplate', label: 'Pipeline Templates' },
      { to: '/stages', icon: 'Columns', label: 'Stages' },
    ],
  },
  {
    id: 'evaluation',
    label: 'Evaluation',
    simpleMode: true,
    defaultCollapsed: true,
    items: [
      { to: '/evals/editor', icon: 'CheckSquare', label: 'Evals' },
      { to: '/evals/proposals', icon: 'Clipboard', label: 'Eval Proposals' },
      { to: '/variants/compare', icon: 'GitFork', label: 'Variants' },
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
      { to: '/settings/remy', icon: 'Bot', label: 'Remy Skills' },
      { to: '/settings/observability', icon: 'Eye', label: 'Observability' },
      { to: '/settings/error-forwarders', icon: 'AlertTriangle', label: 'Error Forwarders' },
    ],
  },
  {
    id: 'schemas',
    label: 'Schemas',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/schemas', icon: 'Database', label: 'Schemas' },
      { to: '/schemas/editor', icon: 'Database', label: 'Schema Editor' },
      { to: '/schemas/infer', icon: 'Database', label: 'Schema Inference' },
    ],
  },
  {
    id: 'admin',
    label: 'Admin',
    simpleMode: false,
    defaultCollapsed: true,
    items: [
      { to: '/admin/users', icon: 'UserCircle', label: 'Users' },
      { to: '/admin/audit', icon: 'FileText', label: 'Audit Log' },
      { to: '/admin/connectors', icon: 'Plug', label: 'Connectors' },
      { to: '/admin/model-backends', icon: 'Cpu', label: 'Model Backends' },
      { to: '/admin/node-categories', icon: 'Tag', label: 'Node Categories' },
      { to: '/admin/views', icon: 'Eye', label: 'Saved Views' },
      { to: '/admin/costs', icon: 'DollarSign', label: 'Cost Breakdown' },
      { to: '/admin/costs/limits', icon: 'CreditCard', label: 'Spend Limits' },
      { to: '/admin/costs/controls', icon: 'SlidersHorizontal', label: 'Cost Controls' },
      { to: '/admin/run-retention', icon: 'Clock', label: 'Run Retention' },
      { to: '/admin/feature-flags', icon: 'Flag', label: 'Feature Flags' },
      { to: '/admin/org', icon: 'Building', label: 'Org Settings' },
      { to: '/admin/plugins', icon: 'Puzzle', label: 'Plugins' },
      { to: '/admin/api-changelog', icon: 'History', label: 'Changelog' },
      { to: '/admin/teams/comparison', icon: 'BarChart', label: 'Team Comparison' },
      { to: '/admin/environments', icon: 'Container', label: 'Environments' },
      { to: '/admin/remy', icon: 'Bot', label: 'Remy Config' },
      { to: '/admin/notification-delivery', icon: 'Bell', label: 'Notification Delivery' },
      { to: '/admin/errors', icon: 'AlertTriangle', label: 'Error Dashboard' },
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
