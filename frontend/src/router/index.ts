import { formatApiError } from '../lib/api/formatError'
import { decodeJwtPayload } from '../lib/jwt'

import { createRouter, createWebHistory } from 'vue-router'
import { getAccessToken } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import manifest from '@/manifest.yaml'
import LoginView from '../views/LoginView.vue'
import AuthCallbackView from '../views/AuthCallbackView.vue'

declare module 'vue-router' {
  interface RouteMeta {
    requiresSystemAdmin?: boolean
    breadcrumb?: string
    parent?: string
    testid?: string
    requiredRoles?: string[]
    requiredTier?: string
    requiredPermissions?: string[]
    featureFlag?: string
    visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
    public?: boolean
    bare?: boolean
  }
}

interface ManifestEntry {
  name: string
  breadcrumb: string
  parent: string | null
  testid: string
  required_roles: string[] | null
  required_tier: string
  required_permissions: string[] | null
  feature_flag: string | null
  visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
}

const manifestRoutes = (manifest as { routes?: Record<string, ManifestEntry> })?.routes ?? {}
const manifestByName = new Map<string, ManifestEntry & { path: string }>()
const manifestPathToName = new Map<string, string>()
for (const [path, entry] of Object.entries(manifestRoutes)) {
  if (entry?.name) {
    manifestByName.set(entry.name, { ...entry, path })
    manifestPathToName.set(path, entry.name)
  }
}

const AnalyticsView = () => import('../views/AnalyticsView.vue')
const DashboardView = () => import('../views/DashboardView.vue')
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
const VariantBatchCompareView = () => import('../views/VariantBatchCompareView.vue')
const ABTestModelsView = () => import('../views/ABTestModelsView.vue')
const RunsListView = () => import('../views/RunsListView.vue')
const RunDetailView = () => import('../views/RunDetailView.vue')
const AgentOutputDiffView = () => import('../views/AgentOutputDiffView.vue')
const AdminAuditView = () => import('../views/AdminAuditView.vue')
const AdminFeatureFlagsView = () => import('../views/AdminFeatureFlagsView.vue')
const AdminPluginsView = () => import('../views/AdminPluginsView.vue')
const PipelineEditorView = () => import('../views/PipelineEditorView.vue')
const CompositeEditorView = () => import('../views/pipeline/CompositeEditorView.vue')
const CopyPipelineWizard = () => import('../views/CopyPipelineWizard.vue')
// removed: PipelineTemplateGallery — merged into /library
const AdminUsersView = () => import('../views/AdminUsersView.vue')
const AdminSpendLimitsView = () => import('../views/AdminSpendLimitsView.vue')
const AdminCostBreakdownView = () => import('../views/AdminCostBreakdownView.vue')
const AdminCostControlsView = () => import('../views/AdminCostControlsView.vue')
const CostComponentsView = () => import('../views/CostComponentsView.vue')
const AdminConnectorsView = () => import('../views/AdminConnectorsView.vue')
const AdminNodeCategoriesView = () => import('../views/AdminNodeCategoriesView.vue')
const AdminViewsView = () => import('../views/AdminViewsView.vue')
const AdminModelBackendsView = () => import('../views/AdminModelBackendsView.vue')
const AdminOrgSettingsView = () => import('../views/AdminOrgSettingsView.vue')
const AdminRunRetentionView = () => import('../views/AdminRunRetentionView.vue')
const AdminSandboxConcurrencyView = () => import('../views/AdminSandboxConcurrencyView.vue')
const NotificationsPage = () => import('../views/NotificationsPage.vue')
const MyProfileView = () => import('../views/MyProfileView.vue')
const SettingsLicenseView = () => import('../views/SettingsLicenseView.vue')
const SettingsMcpView = () => import('../views/SettingsMcpView.vue')
const SettingsTriggersView = () => import('../views/SettingsTriggersView.vue')
const SettingsGuardrailsView = () => import('../views/SettingsGuardrailsView.vue')
const SettingsHitlReviewView = () => import('../views/SettingsHitlReviewView.vue')
const AdminNotificationDeliveryLogView = () => import('../views/AdminNotificationDeliveryLogView.vue')
const AdminHousekeepingView = () => import('../views/AdminHousekeepingView.vue')
const AdminSystemOrgsView = () => import('../views/AdminSystemOrgsView.vue')
const AdminSystemConfigView = () => import('../views/AdminSystemConfigView.vue')
const AdminProductAnalyticsView = () => import('../views/AdminProductAnalyticsView.vue')
const AdminRemyView = () => import('../views/AdminRemyView.vue')
const AdminErrorsView = () => import('../views/AdminErrorsView.vue')
const AdminErrorDetailView = () => import('../views/AdminErrorDetailView.vue')
const UserRemySkillsView = () => import('../views/UserRemySkillsView.vue')
const SettingsEmailView = () => import('../views/SettingsEmailView.vue')
const SettingsErrorForwardersView = () => import('../views/SettingsErrorForwardersView.vue')
const SettingsMonitorConfigView = () => import('../views/SettingsMonitorConfigView.vue')
const PipelineListView = () => import('../views/PipelineListView.vue')
const LifecycleMapEditorView = () => import('../views/lifecycle-map/LifecycleMapEditorView.vue')
const ModelBackendSetupView = () => import('../views/setup/ModelBackendSetupView.vue')
const LifecycleMapList = () => import('../views/lifecycle-map/LifecycleMapList.vue')
const LifecycleMapView = () => import('../views/lifecycle-map/LifecycleMapView.vue')
const DevMetricsView = () => import('../views/DevMetricsView.vue')
const EnvironmentProfileList = () => import('../views/environment-profiles/EnvironmentProfileList.vue')
const EnvironmentProfileForm = () => import('../views/environment-profiles/EnvironmentProfileForm.vue')
const ParameterSchemasView = () => import('../views/ParameterSchemasView.vue')
const OAuthConsentView = () => import('../views/OAuthConsentView.vue')
const RemyOnlyView = () => import('../views/RemyOnlyView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: LoginView,
    },
    {
      // SSO (OIDC/SAML) success handoff: the backend redirects the browser to
      // /auth/callback#access_token=...&refresh_token=... after a successful
      // provider callback. This public route consumes the fragment tokens,
      // stores them, strips them from the URL, and redirects to the dashboard.
      // Public so the auth guard does not bounce an unauthenticated browser
      // back to /login before the tokens are persisted.
      path: '/auth/callback',
      name: 'auth-callback',
      component: AuthCallbackView,
      meta: { public: true, breadcrumb: 'Signing in' },
    },
    {
      // OAuth browser consent route (ADR 017 A1b): the 302 target of
      // /mcp/oauth/authorize. Public — anonymous users must be able to land
      // here and sign in before approving (the authenticated approve POST IS
      // the consent).
      path: '/oauth/authorize',
      name: 'oauth-authorize',
      component: OAuthConsentView,
      meta: { public: true, breadcrumb: 'Authorize' },
    },
    {
      path: '/',
      name: 'dashboard',
      component: DashboardView,
    },
    {
      path: '/analytics',
      name: 'analytics',
      component: AnalyticsView,
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
      meta: { breadcrumb: 'Create Pipeline', parent: 'library' },
    },
    {
      path: '/settings/email',
      name: 'settings-email',
      component: SettingsEmailView,
    },
    {
      path: '/settings/error-forwarders',
      name: 'settings-error-forwarders',
      component: SettingsErrorForwardersView,
    },
    {
      path: '/settings/monitoring',
      name: 'settings-monitoring',
      component: SettingsMonitorConfigView,
    },
    {
      path: '/settings/observability',
      name: 'settings-observability',
      component: SettingsObservabilityView,
    },
    {
      path: '/notifications',
      name: 'notifications',
      component: NotificationsPage,
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
      path: '/settings/guardrails',
      name: 'settings-guardrails',
      component: SettingsGuardrailsView,
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
      meta: { breadcrumb: 'Onboarding', parent: 'dashboard' },
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
      path: '/variants/compare/:batchId',
      name: 'variant-compare-detail',
      component: VariantBatchCompareView,
      props: true,
      meta: { breadcrumb: 'Variant Batch Compare', parent: 'variant-compare', testid: 'variant-batch-compare' },
    },
    {
      path: '/variants/ab-test',
      name: 'ab-test-models',
      component: ABTestModelsView,
    },
    {
      path: '/runs',
      name: 'runs-list',
      component: RunsListView,
    },
    {
      path: '/runs/diff',
      name: 'runs-diff',
      component: AgentOutputDiffView,
    },
    {
      path: '/runs/:id',
      name: 'run-detail',
      component: RunDetailView,
    },
    {
      path: '/admin',
      redirect: '/admin/remy',
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
      path: '/admin/costs/components',
      name: 'admin-costs-components',
      component: CostComponentsView,
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
      path: '/admin/sandbox-concurrency',
      name: 'admin-sandbox-concurrency',
      component: AdminSandboxConcurrencyView,
    },
    {
      path: '/admin/parameter-schemas',
      name: 'admin-parameter-schemas',
      component: ParameterSchemasView,
    },
    {
      path: '/admin/plugins',
      name: 'admin-plugins',
      component: AdminPluginsView,
    },
    {
      path: '/admin/notification-delivery',
      name: 'admin-notification-delivery',
      component: AdminNotificationDeliveryLogView,
    },
    {
      path: '/admin/housekeeping',
      name: 'admin-housekeeping',
      component: AdminHousekeepingView,
      meta: { requiresSystemAdmin: false, breadcrumb: 'Housekeeping', testid: 'admin-housekeeping' },
    },
    {
      path: '/admin/environments',
      redirect: '/environment-profiles',
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
      path: '/admin/product-analytics',
      name: 'admin-product-analytics',
      component: AdminProductAnalyticsView,
      meta: { breadcrumb: 'Product Analytics', parent: 'dashboard', requiresSystemAdmin: true },
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
      path: '/pipelines/copy',
      name: 'pipeline-copy',
      component: CopyPipelineWizard,
    },
    {
      path: '/pipelines',
      name: 'pipeline-list',
      component: PipelineListView,
    },
    {
      path: '/templates',
      redirect: '/library',
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
      path: '/lifecycle-maps/:id/editor',
      name: 'lifecycle-map-editor',
      component: LifecycleMapEditorView,
      meta: { breadcrumb: 'Lifecycle Map Editor', parent: 'lifecycle-maps' },
    },
    {
      path: '/setup/model-backend/:id',
      name: 'ModelBackendSetup',
      component: ModelBackendSetupView,
      meta: { breadcrumb: 'Complete Setup' },
    },
    {
      path: '/lifecycle-maps',
      name: 'lifecycle-maps',
      component: LifecycleMapList,
    },
    {
      path: '/lifecycle-maps/:id',
      name: 'lifecycle-map-detail',
      component: LifecycleMapView,
    },
    {
      path: '/lifecycle-maps/new',
      name: 'lifecycle-map-new',
      redirect: '/lifecycle-maps',
    },
    {
      path: '/dev/metrics',
      name: 'dev-metrics',
      component: DevMetricsView,
      meta: {
        title: 'Web Vitals',
        testid: 'dev-metrics',
        requiresSystemAdmin: true,
      },
    },
    {
      path: '/environment-profiles',
      name: 'environment-profiles',
      component: EnvironmentProfileList,
    },
    {
      path: '/environment-profiles/new',
      name: 'environment-profiles-new',
      component: EnvironmentProfileForm,
      meta: { breadcrumb: 'New Profile', parent: 'environment-profiles' },
    },
    {
      path: '/environment-profiles/:id',
      redirect: (to) => ({ path: `/environment-profiles/${to.params.id}/edit` }),
    },
    {
      path: '/environment-profiles/:id/edit',
      name: 'environment-profiles-edit',
      component: EnvironmentProfileForm,
      meta: { breadcrumb: 'Edit Profile', parent: 'environment-profiles' },
      props: true,
    },
    {
      path: '/remy',
      name: 'remy-only',
      component: RemyOnlyView,
      meta: { bare: true },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      redirect: '/',
    },
  ],
  scrollBehavior(to, _from, savedPosition) {
    if (savedPosition) return savedPosition
    if (to.hash) {
      return { el: to.hash, behavior: 'smooth' }
    }
    return { top: 0 }
  },
})

router.beforeEach(async (to) => {
  try {
    const routeName = to.name
    if (typeof routeName === 'string') {
      const entry = manifestByName.get(routeName)
      if (entry) {
        to.meta.breadcrumb = entry.breadcrumb
        to.meta.testid = entry.testid
        to.meta.requiredRoles = entry.required_roles ?? undefined
        to.meta.requiredTier = entry.required_tier
        to.meta.requiredPermissions = entry.required_permissions ?? undefined
        to.meta.featureFlag = entry.feature_flag ?? undefined
        to.meta.visibility = entry.visibility ?? undefined
        to.meta.parent = entry.parent
          ? (manifestPathToName.get(entry.parent) ?? entry.parent)
          : undefined
      }
    }

    const token = getAccessToken()
    if (to.meta?.public) {
      return true
    }
    if (to.name === 'login' && token) {
      return { name: 'dashboard' }
    }
    if (to.name !== 'login' && !token) {
      return { name: 'login' }
    }
    if (to.meta?.requiresSystemAdmin || to.meta?.requiredRoles?.length || to.meta?.requiredTier) {
      const payload = decodeJwtPayload(token)
      if (to.meta?.requiresSystemAdmin && !payload?.is_system_admin) {
        return { name: 'dashboard' }
      }
      if (to.meta?.requiredRoles?.length) {
        const orgRole = payload?.org_role
        if (typeof orgRole !== 'string' || !to.meta.requiredRoles.includes(orgRole)) {
          return { name: 'dashboard' }
        }
      }
      if (to.meta?.requiredTier || to.meta?.visibility === 'private_preview' || to.meta?.visibility === 'in_dev') {
        // devMode/tier are populated by planStore.fetchPlan(), which is only
        // kicked off from AppLayout.onMounted — AFTER the initial navigation
        // resolves. A direct load/refresh of a private_preview/in_dev route
        // runs this guard before the plan fetch starts, so devMode is still
        // false and the route is spuriously redirected to the dashboard.
        // Await the plan here so the guard sees the real devMode/tier.
        const planStore = usePlanStore()
        if (!planStore.loaded) {
          await planStore.fetchPlan()
        }
        if (to.meta?.requiredTier && Object.keys(planStore.features).length > 0 && !planStore.isAtMinimumTier(to.meta.requiredTier)) {
          return { name: 'dashboard' }
        }
        if (to.meta?.visibility === 'private_preview' || to.meta?.visibility === 'in_dev') {
          if (!planStore.devMode) {
            return { name: 'dashboard' }
          }
        }
      }
    }

    // Generalised variant comparison workflow (FAR-332): when the
    // `variant_batch_compare` feature flag is ON, the legacy model-only
    // AB Test Models view is HARD-REPLACED by the batch-scoped compare flow.
    // The legacy view stays reachable only while the flag is OFF.
    if (to.name === 'ab-test-models') {
      const planStore = usePlanStore()
      if (!planStore.loaded) {
        await planStore.fetchPlan()
      }
      if (planStore.featureEnabled('variant_batch_compare')) {
        return { name: 'variant-compare' }
      }
    }
  } catch (err) {
    console.error('[router] navigation guard error:', err)
    return { name: 'dashboard' }
  }
})

let _chunkRetryCount = 0
router.afterEach(() => {
  _chunkRetryCount = 0
})
router.onError((err) => {
  console.error('[router] navigation error:', err)
  const msg = formatApiError(err)
  if (/Failed to fetch|error loading dynamically|ChunkLoadError/i.test(msg)) {
    if (_chunkRetryCount < 2) {
      _chunkRetryCount++
      const route = router.currentRoute.value
      router.replace(route.fullPath).catch(() => {})
      return
    }
    window.location.reload()
    return
  }
})

export default router
