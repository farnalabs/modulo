import { createRouter, createWebHistory, type RouteMeta } from 'vue-router'
import { getAccessToken } from '../lib/api/client'

declare module 'vue-router' {
  interface RouteMeta {
    requiresSystemAdmin?: boolean
    breadcrumb?: string
    parent?: string
  }
}

const DashboardView = () => import('../views/DashboardView.vue')
const LoginView = () => import('../views/LoginView.vue')
const LibraryView = () => import('../views/LibraryView.vue')
const LibraryPipelineWizard = () => import('../views/LibraryPipelineWizard.vue')
const SettingsObservabilityView = () => import('../views/SettingsObservabilityView.vue')
const SettingsRateLimitsView = () => import('../views/SettingsRateLimitsView.vue')
const SettingsRuntimeConfigView = () => import('../views/SettingsRuntimeConfigView.vue')
const SettingsSsoView = () => import('../views/SettingsSsoView.vue')
const SettingsTeamsView = () => import('../views/SettingsTeamsView.vue')
const SchemaInferenceView = () => import('../views/SchemaInferenceView.vue')
const SchemaListView = () => import('../views/SchemaListView.vue')
const SchemaEditorView = () => import('../views/SchemaEditorView.vue')
const OnboardingWizard = () => import('../views/OnboardingWizard.vue')
const FeedbackInboxView = () => import('../views/FeedbackInboxView.vue')
const EvalEditorView = () => import('../views/EvalEditorView.vue')
const EvalProposalsQueueView = () => import('../views/EvalProposalsQueueView.vue')
const VariantCompareView = () => import('../views/VariantCompareView.vue')
const ABTestModelsView = () => import('../views/ABTestModelsView.vue')
const RunDetailView = () => import('../views/RunDetailView.vue')
const AgentOutputDiffView = () => import('../views/AgentOutputDiffView.vue')
const AdminAuditView = () => import('../views/AdminAuditView.vue')
const AdminFeatureFlagsView = () => import('../views/AdminFeatureFlagsView.vue')
const AdminPluginsView = () => import('../views/AdminPluginsView.vue')
const ApiChangelogView = () => import('../views/ApiChangelogView.vue')
const TeamComparisonView = () => import('../views/TeamComparisonView.vue')
const StageBoardView = () => import('../views/StageBoardView.vue')
const PipelineEditorView = () => import('../views/PipelineEditorView.vue')
const CompositeEditorView = () => import('../views/pipeline/CompositeEditorView.vue')
const CopyPipelineWizard = () => import('../views/CopyPipelineWizard.vue')
const PipelineTemplateGallery = () => import('../views/PipelineTemplateGallery.vue')
const AdminUsersView = () => import('../views/AdminUsersView.vue')
const AdminSpendLimitsView = () => import('../views/AdminSpendLimitsView.vue')
const AdminCostBreakdownView = () => import('../views/AdminCostBreakdownView.vue')
const AdminCostControlsView = () => import('../views/AdminCostControlsView.vue')
const AdminConnectorsView = () => import('../views/AdminConnectorsView.vue')
const AdminNodeCategoriesView = () => import('../views/AdminNodeCategoriesView.vue')
const AdminViewsView = () => import('../views/AdminViewsView.vue')
const AdminModelBackendsView = () => import('../views/AdminModelBackendsView.vue')
const AdminOrgSettingsView = () => import('../views/AdminOrgSettingsView.vue')
const AdminRunRetentionView = () => import('../views/AdminRunRetentionView.vue')
const NotificationsPage = () => import('../views/NotificationsPage.vue')
const MyProfileView = () => import('../views/MyProfileView.vue')
const SettingsLicenseView = () => import('../views/SettingsLicenseView.vue')
const SettingsMcpView = () => import('../views/SettingsMcpView.vue')
const SettingsTriggersView = () => import('../views/SettingsTriggersView.vue')
const SettingsHitlReviewView = () => import('../views/SettingsHitlReviewView.vue')
const AdminNotificationDeliveryLogView = () => import('../views/AdminNotificationDeliveryLogView.vue')
const AdminEnvironmentProfilesView = () => import('../views/AdminEnvironmentProfilesView.vue')
const AdminSystemOrgsView = () => import('../views/AdminSystemOrgsView.vue')
const AdminSystemConfigView = () => import('../views/AdminSystemConfigView.vue')
const AdminRemyView = () => import('../views/AdminRemyView.vue')
const AdminErrorsView = () => import('../views/AdminErrorsView.vue')
const AdminErrorDetailView = () => import('../views/AdminErrorDetailView.vue')
const UserRemySkillsView = () => import('../views/UserRemySkillsView.vue')
const SettingsErrorForwardersView = () => import('../views/SettingsErrorForwardersView.vue')
const PipelineListView = () => import('../views/PipelineListView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: LoginView,
    },
    {
      path: '/',
      name: 'dashboard',
      component: DashboardView,
      meta: { breadcrumb: 'Dashboard' },
    },
    {
      path: '/dashboard',
      redirect: '/',
    },
    {
      path: '/library',
      name: 'library',
      component: LibraryView,
      meta: { breadcrumb: 'Library', parent: 'dashboard' },
    },
    {
      path: '/library/:id/create-pipeline',
      name: 'library-pipeline-wizard',
      component: LibraryPipelineWizard,
      props: true,
      meta: { breadcrumb: 'Create Pipeline', parent: 'library' },
    },
    {
      path: '/settings/error-forwarders',
      name: 'settings-error-forwarders',
      component: SettingsErrorForwardersView,
      meta: { breadcrumb: 'Error Forwarders', parent: 'dashboard' },
    },
    {
      path: '/settings/observability',
      name: 'settings-observability',
      component: SettingsObservabilityView,
      meta: { breadcrumb: 'Observability', parent: 'dashboard' },
    },
    {
      path: '/notifications',
      name: 'notifications',
      component: NotificationsPage,
      meta: { breadcrumb: 'Notifications', parent: 'dashboard' },
    },
    {
      path: '/settings/teams',
      name: 'settings-teams',
      component: SettingsTeamsView,
      meta: { breadcrumb: 'Teams', parent: 'dashboard' },
    },
    {
      path: '/settings/sso',
      name: 'settings-sso',
      component: SettingsSsoView,
      meta: { breadcrumb: 'SSO', parent: 'dashboard' },
    },
    {
      path: '/settings/rate-limits',
      name: 'settings-rate-limits',
      component: SettingsRateLimitsView,
      meta: { breadcrumb: 'Rate Limits', parent: 'dashboard' },
    },
    {
      path: '/settings/runtime-config',
      name: 'settings-runtime-config',
      component: SettingsRuntimeConfigView,
      meta: { breadcrumb: 'Runtime Config', parent: 'dashboard' },
    },
    {
      path: '/settings/license',
      name: 'settings-license',
      component: SettingsLicenseView,
      meta: { breadcrumb: 'License', parent: 'dashboard' },
    },
    {
      path: '/settings/mcp',
      name: 'settings-mcp',
      component: SettingsMcpView,
      meta: { breadcrumb: 'MCP', parent: 'dashboard' },
    },
    {
      path: '/settings/triggers',
      name: 'settings-triggers',
      component: SettingsTriggersView,
      meta: { breadcrumb: 'Triggers', parent: 'dashboard' },
    },
    {
      path: '/settings/hitl-review',
      name: 'settings-hitl-review',
      component: SettingsHitlReviewView,
      meta: { breadcrumb: 'HITL Review', parent: 'dashboard' },
    },
    {
      path: '/settings/remy',
      name: 'settings-remy',
      component: UserRemySkillsView,
      meta: { breadcrumb: 'Remy Skills', parent: 'dashboard' },
    },
    {
      path: '/schemas',
      name: 'schemas',
      component: SchemaListView,
      meta: { breadcrumb: 'Schemas', parent: 'dashboard' },
    },
    {
      path: '/schemas/editor/:id?',
      name: 'schema-editor',
      component: SchemaEditorView,
      meta: { breadcrumb: 'Schema Editor', parent: 'schemas' },
    },
    {
      path: '/schemas/infer',
      name: 'schema-infer',
      component: SchemaInferenceView,
      meta: { breadcrumb: 'Schema Inference', parent: 'schemas' },
    },
    {
      path: '/onboarding',
      name: 'onboarding',
      component: OnboardingWizard,
      meta: { breadcrumb: 'Onboarding', parent: 'dashboard' },
    },
    {
      path: '/feedback/inbox',
      name: 'feedback-inbox',
      component: FeedbackInboxView,
      meta: { breadcrumb: 'Feedback Inbox', parent: 'dashboard' },
    },
    {
      path: '/evals/editor',
      name: 'eval-editor',
      component: EvalEditorView,
      meta: { breadcrumb: 'Evals', parent: 'dashboard' },
    },
    {
      path: '/evals/proposals',
      name: 'eval-proposals-queue',
      component: EvalProposalsQueueView,
      meta: { breadcrumb: 'Eval Proposals', parent: 'dashboard' },
    },
    {
      path: '/variants/compare',
      name: 'variant-compare',
      component: VariantCompareView,
      meta: { breadcrumb: 'Variants', parent: 'dashboard' },
    },
    {
      path: '/variants/ab-test',
      name: 'ab-test-models',
      component: ABTestModelsView,
      meta: { breadcrumb: 'AB Test Models', parent: 'dashboard' },
    },
    {
      path: '/runs/:id',
      name: 'run-detail',
      component: RunDetailView,
      meta: { breadcrumb: 'Run Detail', parent: 'dashboard' },
    },
    {
      path: '/runs/diff',
      name: 'runs-diff',
      component: AgentOutputDiffView,
      meta: { breadcrumb: 'Output Diff', parent: 'dashboard' },
    },
    {
      path: '/admin/my-profile',
      name: 'my-profile',
      component: MyProfileView,
      meta: { breadcrumb: 'My Profile', parent: 'dashboard' },
    },
    {
      path: '/admin/users',
      name: 'admin-users',
      component: AdminUsersView,
      meta: { breadcrumb: 'Users', parent: 'dashboard' },
    },
    {
      path: '/admin/costs/limits',
      name: 'admin-costs-limits',
      component: AdminSpendLimitsView,
      meta: { breadcrumb: 'Spend Limits', parent: 'admin-costs' },
    },
    {
      path: '/admin/costs',
      name: 'admin-costs',
      component: AdminCostBreakdownView,
      meta: { breadcrumb: 'Cost Overview', parent: 'dashboard' },
    },
    {
      path: '/admin/costs/controls',
      name: 'admin-costs-controls',
      component: AdminCostControlsView,
      meta: { breadcrumb: 'Cost Controls', parent: 'admin-costs' },
    },
    {
      path: '/admin/audit',
      name: 'admin-audit',
      component: AdminAuditView,
      meta: { breadcrumb: 'Audit Log', parent: 'dashboard' },
    },
    {
      path: '/admin/connectors',
      name: 'admin-connectors',
      component: AdminConnectorsView,
      meta: { breadcrumb: 'Connectors', parent: 'dashboard' },
    },
    {
      path: '/admin/node-categories',
      name: 'admin-node-categories',
      component: AdminNodeCategoriesView,
      meta: { breadcrumb: 'Node Categories', parent: 'dashboard' },
    },
    {
      path: '/admin/views',
      name: 'admin-views',
      component: AdminViewsView,
      meta: { breadcrumb: 'Saved Views', parent: 'dashboard' },
    },
    {
      path: '/admin/model-backends',
      name: 'admin-model-backends',
      component: AdminModelBackendsView,
      meta: { breadcrumb: 'Model Backends', parent: 'dashboard' },
    },
    {
      path: '/admin/feature-flags',
      name: 'admin-feature-flags',
      component: AdminFeatureFlagsView,
      meta: { breadcrumb: 'Feature Flags', parent: 'dashboard' },
    },
    {
      path: '/admin/org',
      name: 'admin-org',
      component: AdminOrgSettingsView,
      meta: { breadcrumb: 'Org Settings', parent: 'dashboard' },
    },
    {
      path: '/admin/run-retention',
      name: 'admin-run-retention',
      component: AdminRunRetentionView,
      meta: { breadcrumb: 'Run Retention', parent: 'dashboard' },
    },
    {
      path: '/admin/plugins',
      name: 'admin-plugins',
      component: AdminPluginsView,
      meta: { breadcrumb: 'Plugins', parent: 'dashboard' },
    },
    {
      path: '/admin/api-changelog',
      name: 'api-changelog',
      component: ApiChangelogView,
      meta: { breadcrumb: 'API Changelog', parent: 'dashboard' },
    },
    {
      path: '/admin/teams/comparison',
      name: 'team-comparison',
      component: TeamComparisonView,
      meta: { breadcrumb: 'Team Comparison', parent: 'dashboard' },
    },
    {
      path: '/admin/notification-delivery',
      name: 'admin-notification-delivery',
      component: AdminNotificationDeliveryLogView,
      meta: { breadcrumb: 'Notification Log', parent: 'dashboard' },
    },
    {
      path: '/admin/environments',
      name: 'admin-environments',
      component: AdminEnvironmentProfilesView,
      meta: { breadcrumb: 'Environments', parent: 'dashboard' },
    },
    {
      path: '/admin/system/orgs',
      name: 'admin-system-orgs',
      component: AdminSystemOrgsView,
      meta: { breadcrumb: 'Organisations', parent: 'dashboard', requiresSystemAdmin: true },
    },
    {
      path: '/admin/system/config',
      name: 'admin-system-config',
      component: AdminSystemConfigView,
      meta: { breadcrumb: 'System Config', parent: 'dashboard', requiresSystemAdmin: true },
    },
    {
      path: '/admin/errors',
      name: 'admin-errors',
      component: AdminErrorsView,
      meta: { breadcrumb: 'Error Dashboard', parent: 'dashboard' },
    },
    {
      path: '/admin/errors/:id',
      name: 'admin-error-detail',
      component: AdminErrorDetailView,
      meta: { breadcrumb: 'Error Detail', parent: 'admin-errors' },
    },
    {
      path: '/admin/remy',
      name: 'admin-remy',
      component: AdminRemyView,
      meta: { breadcrumb: 'Remy Config', parent: 'dashboard' },
    },
    {
      path: '/stages',
      name: 'stages',
      component: StageBoardView,
      meta: { breadcrumb: 'Stages Board', parent: 'dashboard' },
    },
    {
      path: '/pipelines/copy',
      name: 'pipeline-copy',
      component: CopyPipelineWizard,
      meta: { breadcrumb: 'Copy Pipeline', parent: 'library' },
    },
    {
      path: '/pipelines',
      name: 'pipeline-list',
      component: PipelineListView,
      meta: { breadcrumb: 'Pipelines', parent: 'library' },
    },
    {
      path: '/templates',
      name: 'pipeline-templates',
      component: PipelineTemplateGallery,
      meta: { breadcrumb: 'Templates', parent: 'library' },
    },
    {
      path: '/pipelines/:id/editor',
      name: 'pipeline-editor',
      component: PipelineEditorView,
      meta: { breadcrumb: 'Pipeline Editor', parent: 'library' },
    },
    {
      path: '/composites/:id/editor',
      name: 'composite-editor',
      component: CompositeEditorView,
      meta: { breadcrumb: 'Composite Editor', parent: 'library' },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      redirect: '/',
    },
  ],
  scrollBehavior() {
    return { top: 0 }
  },
})

router.onError((err) => {
  console.error('[router] unhandled error:', err)
})

router.beforeEach((to) => {
  if (to.name === 'login' && getAccessToken()) {
    return { name: 'dashboard' }
  }
  if (to.name !== 'login' && !getAccessToken()) {
    return { name: 'login' }
  }
  if (to.meta?.requiresSystemAdmin) {
    const token = getAccessToken()
    if (!token) return { name: 'login' }
    try {
      const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
      if (payload.is_system_admin !== true) {
        return { name: 'dashboard' }
      }
    } catch {
      return { name: 'dashboard' }
    }
  }
})

export default router
