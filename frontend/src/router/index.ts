import { createRouter, createWebHistory } from 'vue-router'
import { getAccessToken } from '../lib/api/client'

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
import OnboardingWizard from '../views/OnboardingWizard.vue'
import FeedbackInboxView from '../views/FeedbackInboxView.vue'
import EvalEditorView from '../views/EvalEditorView.vue'
import EvalProposalsQueueView from '../views/EvalProposalsQueueView.vue'
import VariantCompareView from '../views/VariantCompareView.vue'
import ABTestModelsView from '../views/ABTestModelsView.vue'
import RunDetailView from '../views/RunDetailView.vue'
import AdminAuditView from '../views/AdminAuditView.vue'
import AdminFeatureFlagsView from '../views/AdminFeatureFlagsView.vue'
import ApiChangelogView from '../views/ApiChangelogView.vue'
import TeamComparisonView from '../views/TeamComparisonView.vue'
import PipelineEditorView from '../views/PipelineEditorView.vue'
import AdminUsersView from '../views/AdminUsersView.vue'
import AdminSpendLimitsView from '../views/AdminSpendLimitsView.vue'
import AdminCostBreakdownView from '../views/AdminCostBreakdownView.vue'
import AdminConnectorsView from '../views/AdminConnectorsView.vue'
import AdminNodeCategoriesView from '../views/AdminNodeCategoriesView.vue'
import MyProfileView from '../views/MyProfileView.vue'
import SettingsLicenseView from '../views/SettingsLicenseView.vue'
import SettingsMcpView from '../views/SettingsMcpView.vue'

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
      path: '/admin/feature-flags',
      name: 'admin-feature-flags',
      component: AdminFeatureFlagsView,
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
      path: '/pipelines/:id/editor',
      name: 'pipeline-editor',
      component: PipelineEditorView,
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
})

export default router
