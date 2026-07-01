import { createRouter, createWebHistory, type RouteMeta } from 'vue-router'
import { getAccessToken } from '../lib/api/client'

declare module 'vue-router' {
  interface RouteMeta {
    requiresSystemAdmin?: boolean
  }
}

import DashboardView from '../views/DashboardView.vue'
import LoginView from '../views/LoginView.vue'
import LibraryView from '../views/LibraryView.vue'
import LibraryPipelineWizard from '../views/LibraryPipelineWizard.vue'
import SettingsObservabilityView from '../views/SettingsObservabilityView.vue'
import SettingsRateLimitsView from '../views/SettingsRateLimitsView.vue'
import SettingsRuntimeConfigView from '../views/SettingsRuntimeConfigView.vue'
import SettingsSsoView from '../views/SettingsSsoView.vue'
import SettingsTeamsView from '../views/SettingsTeamsView.vue'
import SchemaInferenceView from '../views/SchemaInferenceView.vue'
import SchemaListView from '../views/SchemaListView.vue'
import SchemaEditorView from '../views/SchemaEditorView.vue'
import OnboardingWizard from '../views/OnboardingWizard.vue'
import FeedbackInboxView from '../views/FeedbackInboxView.vue'
import EvalEditorView from '../views/EvalEditorView.vue'
import EvalProposalsQueueView from '../views/EvalProposalsQueueView.vue'
import VariantCompareView from '../views/VariantCompareView.vue'
import ABTestModelsView from '../views/ABTestModelsView.vue'
import RunDetailView from '../views/RunDetailView.vue'
import AgentOutputDiffView from '../views/AgentOutputDiffView.vue'
import AdminAuditView from '../views/AdminAuditView.vue'
import AdminFeatureFlagsView from '../views/AdminFeatureFlagsView.vue'
import AdminPluginsView from '../views/AdminPluginsView.vue'
import ApiChangelogView from '../views/ApiChangelogView.vue'
import TeamComparisonView from '../views/TeamComparisonView.vue'
import StageBoardView from '../views/StageBoardView.vue'
import PipelineEditorView from '../views/PipelineEditorView.vue'
import CompositeEditorView from '../views/pipeline/CompositeEditorView.vue'
import CopyPipelineWizard from '../views/CopyPipelineWizard.vue'
import PipelineTemplateGallery from '../views/PipelineTemplateGallery.vue'
import AdminUsersView from '../views/AdminUsersView.vue'
import AdminSpendLimitsView from '../views/AdminSpendLimitsView.vue'
import AdminCostBreakdownView from '../views/AdminCostBreakdownView.vue'
import AdminCostControlsView from '../views/AdminCostControlsView.vue'
import AdminConnectorsView from '../views/AdminConnectorsView.vue'
import AdminNodeCategoriesView from '../views/AdminNodeCategoriesView.vue'
import AdminViewsView from '../views/AdminViewsView.vue'
import AdminModelBackendsView from '../views/AdminModelBackendsView.vue'
import AdminOrgSettingsView from '../views/AdminOrgSettingsView.vue'
import AdminRunRetentionView from '../views/AdminRunRetentionView.vue'
import MyProfileView from '../views/MyProfileView.vue'
import SettingsLicenseView from '../views/SettingsLicenseView.vue'
import SettingsMcpView from '../views/SettingsMcpView.vue'
import SettingsTriggersView from '../views/SettingsTriggersView.vue'
import SettingsHitlReviewView from '../views/SettingsHitlReviewView.vue'
import AdminNotificationDeliveryLogView from '../views/AdminNotificationDeliveryLogView.vue'
import AdminEnvironmentProfilesView from '../views/AdminEnvironmentProfilesView.vue'
import AdminSystemOrgsView from '../views/AdminSystemOrgsView.vue'
import AdminSystemConfigView from '../views/AdminSystemConfigView.vue'
import AdminRemyView from '../views/AdminRemyView.vue'
import AdminErrorsView from '../views/AdminErrorsView.vue'
import AdminErrorDetailView from '../views/AdminErrorDetailView.vue'
import UserRemySkillsView from '../views/UserRemySkillsView.vue'
import SettingsErrorForwardersView from '../views/SettingsErrorForwardersView.vue'

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
    },
    {
      path: '/dashboard',
      redirect: '/',
    },
    {
      path: '/library',
      name: 'library',
      component: LibraryView,
    },
    {
      path: '/library/:id/create-pipeline',
      name: 'library-pipeline-wizard',
      component: LibraryPipelineWizard,
      props: true,
    },
    {
      path: '/settings/error-forwarders',
      name: 'settings-error-forwarders',
      component: SettingsErrorForwardersView,
    },
    {
      path: '/settings/observability',
      name: 'settings-observability',
      component: SettingsObservabilityView,
    },
    {
      path: '/settings/teams',
      name: 'settings-teams',
      component: SettingsTeamsView,
    },
    {
      path: '/settings/sso',
      name: 'settings-sso',
      component: SettingsSsoView,
    },
    {
      path: '/settings/rate-limits',
      name: 'settings-rate-limits',
      component: SettingsRateLimitsView,
    },
    {
      path: '/settings/runtime-config',
      name: 'settings-runtime-config',
      component: SettingsRuntimeConfigView,
    },
    {
      path: '/settings/license',
      name: 'settings-license',
      component: SettingsLicenseView,
    },
    {
      path: '/settings/mcp',
      name: 'settings-mcp',
      component: SettingsMcpView,
    },
    {
      path: '/settings/triggers',
      name: 'settings-triggers',
      component: SettingsTriggersView,
    },
    {
      path: '/settings/hitl-review',
      name: 'settings-hitl-review',
      component: SettingsHitlReviewView,
    },
    {
      path: '/settings/remy',
      name: 'settings-remy',
      component: UserRemySkillsView,
    },
    {
      path: '/schemas',
      name: 'schemas',
      component: SchemaListView,
    },
    {
      path: '/schemas/editor/:id?',
      name: 'schema-editor',
      component: SchemaEditorView,
    },
    {
      path: '/schemas/infer',
      name: 'schema-infer',
      component: SchemaInferenceView,
    },
    {
      path: '/onboarding',
      name: 'onboarding',
      component: OnboardingWizard,
    },
    {
      path: '/feedback/inbox',
      name: 'feedback-inbox',
      component: FeedbackInboxView,
    },
    {
      path: '/evals/editor',
      name: 'eval-editor',
      component: EvalEditorView,
    },
    {
      path: '/evals/proposals',
      name: 'eval-proposals-queue',
      component: EvalProposalsQueueView,
    },
    {
      path: '/variants/compare',
      name: 'variant-compare',
      component: VariantCompareView,
    },
    {
      path: '/variants/ab-test',
      name: 'ab-test-models',
      component: ABTestModelsView,
    },
    {
      path: '/runs/:id',
      name: 'run-detail',
      component: RunDetailView,
    },
    {
      path: '/runs/diff',
      name: 'runs-diff',
      component: AgentOutputDiffView,
    },
    {
      path: '/admin/my-profile',
      name: 'my-profile',
      component: MyProfileView,
    },
    {
      path: '/admin/users',
      name: 'admin-users',
      component: AdminUsersView,
    },
    {
      path: '/admin/costs/limits',
      name: 'admin-costs-limits',
      component: AdminSpendLimitsView,
    },
    {
      path: '/admin/costs',
      name: 'admin-costs',
      component: AdminCostBreakdownView,
    },
    {
      path: '/admin/costs/controls',
      name: 'admin-costs-controls',
      component: AdminCostControlsView,
    },
    {
      path: '/admin/audit',
      name: 'admin-audit',
      component: AdminAuditView,
    },
    {
      path: '/admin/connectors',
      name: 'admin-connectors',
      component: AdminConnectorsView,
    },
    {
      path: '/admin/node-categories',
      name: 'admin-node-categories',
      component: AdminNodeCategoriesView,
    },
    {
      path: '/admin/views',
      name: 'admin-views',
      component: AdminViewsView,
    },
    {
      path: '/admin/model-backends',
      name: 'admin-model-backends',
      component: AdminModelBackendsView,
    },
    {
      path: '/admin/feature-flags',
      name: 'admin-feature-flags',
      component: AdminFeatureFlagsView,
    },
    {
      path: '/admin/org',
      name: 'admin-org',
      component: AdminOrgSettingsView,
    },
    {
      path: '/admin/run-retention',
      name: 'admin-run-retention',
      component: AdminRunRetentionView,
    },
    {
      path: '/admin/plugins',
      name: 'admin-plugins',
      component: AdminPluginsView,
    },
    {
      path: '/admin/api-changelog',
      name: 'api-changelog',
      component: ApiChangelogView,
    },
    {
      path: '/admin/teams/comparison',
      name: 'team-comparison',
      component: TeamComparisonView,
    },
    {
      path: '/admin/notification-delivery',
      name: 'admin-notification-delivery',
      component: AdminNotificationDeliveryLogView,
    },
    {
      path: '/admin/environments',
      name: 'admin-environments',
      component: AdminEnvironmentProfilesView,
    },
    {
      path: '/admin/system/orgs',
      name: 'admin-system-orgs',
      component: AdminSystemOrgsView,
      meta: { requiresSystemAdmin: true },
    },
    {
      path: '/admin/system/config',
      name: 'admin-system-config',
      component: AdminSystemConfigView,
      meta: { requiresSystemAdmin: true },
    },
    {
      path: '/admin/errors',
      name: 'admin-errors',
      component: AdminErrorsView,
    },
    {
      path: '/admin/errors/:id',
      name: 'admin-error-detail',
      component: AdminErrorDetailView,
    },
    {
      path: '/admin/remy',
      name: 'admin-remy',
      component: AdminRemyView,
    },
    {
      path: '/stages',
      name: 'stages',
      component: StageBoardView,
    },
    {
      path: '/pipelines/copy',
      name: 'pipeline-copy',
      component: CopyPipelineWizard,
    },
    {
      path: '/pipelines',
      redirect: '/library',
    },
    {
      path: '/templates',
      name: 'pipeline-templates',
      component: PipelineTemplateGallery,
    },
    {
      path: '/pipelines/:id/editor',
      name: 'pipeline-editor',
      component: PipelineEditorView,
    },
    {
      path: '/composites/:id/editor',
      name: 'composite-editor',
      component: CompositeEditorView,
    },
  ],
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
      const payload = JSON.parse(atob(token.split('.')[1]))
      if (payload.is_system_admin !== true) {
        return { name: 'dashboard' }
      }
    } catch {
      return { name: 'dashboard' }
    }
  }
})

export default router
