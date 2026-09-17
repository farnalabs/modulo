import { formatApiError } from '../lib/api/formatError'
import { decodeJwtPayload } from '../lib/jwt'

import { createRouter, createWebHistory } from 'vue-router'
import { getAccessToken } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import manifest from '@/manifest.yaml'
import LoginView from '../views/LoginView.vue'
import OrgLoginView from '../views/OrgLoginView.vue'
import AuthCallbackView from '../views/AuthCallbackView.vue'
import { resolveDemoEntry } from '../lib/api/demo'

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
const CollectionCreateView = () => import('../views/CollectionCreateView.vue')
const CollectionDetailView = () => import('../views/CollectionDetailView.vue')
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
const AdminRunnersView = () => import('../views/AdminRunnersView.vue')
const RunnersProfilesTab = () => import('../views/runners/RunnersProfilesTab.vue')
const RunnersConcurrencyTab = () => import('../views/runners/RunnersConcurrencyTab.vue')
const EnvironmentProfileForm = () => import('../views/environment-profiles/EnvironmentProfileForm.vue')
const ParameterSchemasView = () => import('../views/ParameterSchemasView.vue')
const OAuthConsentView = () => import('../views/OAuthConsentView.vue')
const DemoView = () => import('../views/DemoView.vue')
const AcceptInviteView = () => import('../views/AcceptInviteView.vue')
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
      // Per-org login (FAR-863): bookmarkable, deep-linkable, refresh-safe.
      // The view resolves the org on mount via GET /api/v1/auth/org-login/{slug}.
      // A not-found response shows a generic error — never reveals whether the
      // slug exists (tenancy boundary).
      path: '/login/:slug',
      name: 'org-login',
      component: OrgLoginView,
      props: true,
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
      // Demo auto-login (FAR-535): public one-shot route. The beforeEach guard
      // intercepts /demo and performs the hand-off pre-mount (clear stored
      // auth → POST /api/v1/auth/demo → store the short-lived read-only demo
      // token), redirecting to the dashboard — or to /login on failure, never
      // surfacing an error that reveals demo internals. The component is a
      // defensive splash only; the guard always redirects before it mounts.
      path: '/demo',
      name: 'demo',
      component: DemoView,
      meta: { public: true, breadcrumb: 'Demo' },
    },
    {
      // One-time invite enrollment (FAR-461): <origin>/accept-invite#token=...
      // Public by design — the token IS the credential; no session exists yet.
      // The view sets a password and then hands off to /login.
      path: '/accept-invite',
      name: 'accept-invite',
      component: AcceptInviteView,
      meta: { public: true, breadcrumb: 'Accept Invitation' },
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
      path: '/library/collections/new',
      name: 'library-collection-create',
      component: CollectionCreateView,
      meta: { breadcrumb: 'New Collection', parent: 'library' },
    },
    {
      path: '/library/collections/:id',
      name: 'library-collection-detail',
      component: CollectionDetailView,
      props: true,
      meta: { breadcrumb: 'Collection', parent: 'library' },
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
      // FAR-591 D5: the Runners page replaced the standalone Sandbox
      // Concurrency surface (now the Concurrency tab).
      path: '/admin/sandbox-concurrency',
      redirect: '/admin/runners/concurrency',
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
      // FAR-591 D5: the Runners page (CONFIGURE). Route-per-tab:
      // /admin/runners/profiles + /admin/runners/concurrency share the
      // AdminRunnersView layout (PageTabs + persistent status strip).
      // Profile create/edit keep real nested editor routes under the
      // profiles tab so legacy deep links map 1:1.
      path: '/admin/runners/profiles',
      name: 'admin-runners-profiles',
      component: AdminRunnersView,
      children: [
        {
          path: '',
          component: RunnersProfilesTab,
        },
        {
          path: 'new',
          name: 'admin-runners-profile-new',
          component: EnvironmentProfileForm,
          meta: { breadcrumb: 'New Profile', parent: 'admin-runners-profiles' },
        },
        {
          path: ':id',
          redirect: (to) => ({ path: `/admin/runners/profiles/${String(to.params.id)}/edit` }),
        },
        {
          path: ':id/edit',
          name: 'admin-runners-profile-edit',
          component: EnvironmentProfileForm,
          meta: { breadcrumb: 'Edit Profile', parent: 'admin-runners-profiles' },
          props: true,
        },
      ],
    },
    {
      path: '/admin/runners/concurrency',
      name: 'admin-runners-concurrency',
      component: AdminRunnersView,
      children: [
        {
          path: '',
          component: RunnersConcurrencyTab,
        },
      ],
    },
    {
      // FAR-591 D5: the Environment Profiles surface folded into the
      // Runners page — every legacy route keeps working via redirect.
      path: '/environment-profiles',
      redirect: '/admin/runners/profiles',
    },
    {
      path: '/environment-profiles/new',
      redirect: '/admin/runners/profiles/new',
    },
    {
      path: '/environment-profiles/:id',
      redirect: (to) => ({ path: `/admin/runners/profiles/${String(to.params.id)}` }),
    },
    {
      path: '/environment-profiles/:id/edit',
      redirect: (to) => ({ path: `/admin/runners/profiles/${String(to.params.id)}/edit` }),
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

/**
 * Hydrate to.meta fields from the manifest registry so downstream guards
 * and breadcrumb logic see a single canonical source.  Manifest entries
 * are keyed by route *name*; the parent path is resolved to a route name
 * via manifestPathToName.
 */
export function hydrateManifestMeta(to: Parameters<Parameters<typeof router.beforeEach>[0]>[0]): void {
  const routeName = to.name
  if (typeof routeName !== 'string') return

  const entry = manifestByName.get(routeName)
  if (!entry) return

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

/**
 * Enforce role, tier, and devMode/visibility access rules.  Returns a
 * redirect route-object when access is denied, or `true` when the caller
 * may continue.  The plan store is lazily fetched so the guard sees the
 * real tier — the first navigation resolves *before* AppLayout.onMounted
 * kicks off `fetchPlan()`.
 */
export async function enforceRoleTierVisibility(
  to: Parameters<Parameters<typeof router.beforeEach>[0]>[0],
  token: string,
): Promise<{ name: string } | true> {
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

  const needsTierOrVisibility =
    to.meta?.requiredTier ||
    to.meta?.visibility === 'private_preview' ||
    to.meta?.visibility === 'in_dev'

  if (needsTierOrVisibility) {
    const planStore = usePlanStore()
    if (!planStore.loaded) {
      await planStore.fetchPlan()
    }
    if (
      to.meta?.requiredTier &&
      Object.keys(planStore.features).length > 0 &&
      !planStore.isAtMinimumTier(to.meta.requiredTier)
    ) {
      return { name: 'dashboard' }
    }
    const isPrivateOrDev =
      to.meta?.visibility === 'private_preview' || to.meta?.visibility === 'in_dev'
    if (isPrivateOrDev && !planStore.devMode) {
      return { name: 'dashboard' }
    }
  }

  return true
}

// Note: the phantom `variant_batch_compare` feature flag was removed in FAR-926.

router.beforeEach(async (to) => {
  try {
    // FAR-535 demo auto-login: /demo never renders as a page. The guard owns
    // the hand-off so it runs pre-mount — main.ts awaits router.isReady()
    // BEFORE App mounts, so an anonymous visitor hitting /demo lands straight
    // on the dashboard with the demo session already stored (App.vue's
    // unauthenticated LoginView branch never has a chance to flash).
    // qa iter 1: a LIVE session must never be torn down by merely visiting
    // /demo — previously every navigation re-ran the hand-off, so
    // Back/Forward to /demo logged a real user out, raced the auto-login
    // recovery listener, and re-minted a token (burning the 10/hour budget).
    // With a token present (demo flag set or a real session) the visitor goes
    // straight to the dashboard; only a tokenless browser runs the hand-off.
    // qa iter 2: the rule lives in resolveDemoEntry (lib/api/demo.ts) so the
    // guard and DemoView's fallback cannot drift.
    if (to.name === 'demo') {
      return { name: await resolveDemoEntry() }
    }

    hydrateManifestMeta(to)

    const token = getAccessToken()
    if (to.meta?.public) return true
    if ((to.name === 'login' || to.name === 'org-login') && token) return { name: 'dashboard' }
    if (to.name === 'login' || to.name === 'org-login') return true // login page without token — allowed
    if (!token) return { name: 'login' }

    // Role / tier / visibility enforcement
    if (to.meta?.requiresSystemAdmin || to.meta?.requiredRoles?.length || to.meta?.requiredTier) {
      const denied = await enforceRoleTierVisibility(to, token)
      if (denied !== true) return denied
    }

    // Manifest-declared route flags (FAR-656): a route whose manifest entry
    // carries feature_flag is reachable only while that flag resolves true —
    // a disabled flag redirects to the dashboard. navigation.ts filters the
    // sidebar item on the same flag so nav and route stay consistent.
    if (to.meta?.featureFlag) {
      const planStore = usePlanStore()
      if (!planStore.loaded) {
        await planStore.fetchPlan()
      }
      if (!planStore.featureEnabled(to.meta.featureFlag)) {
        return { name: 'dashboard' }
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
