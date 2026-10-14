<template>
  <div class="page-wide">
    <PageHeader
      :title="$t('views.GovernanceStackView.title')"
      :subtitle="$t('views.GovernanceStackView.subtitle')"
    />

    <p class="mt-4 max-w-3xl text-sm text-muted-foreground">
      {{ $t('views.GovernanceStackView.intro') }}
    </p>

    <div class="mt-6 space-y-4">
      <article
        v-for="level in levels"
        :key="level.id"
        class="card p-6"
        :data-testid="`governance-level-${level.id}`"
      >
        <header>
          <h2 class="text-base font-semibold">
            <span class="mr-2 text-muted-foreground" aria-hidden="true">{{ level.order }}.</span>{{ $t(level.titleKey) }}
          </h2>
          <p class="mt-1 text-sm text-muted-foreground">{{ $t(level.primitiveKey) }}</p>
        </header>

        <p
          v-if="level.noteKey"
          class="mt-3 rounded-lg border border-dashed border-border bg-muted/30 p-3 text-xs text-muted-foreground"
          :data-testid="`governance-level-note-${level.id}`"
        >
          {{ $t(level.noteKey) }}
        </p>

        <FeatureGate
          :feature-name="level.featureName ?? ''"
          :required-tier="level.requiredTier"
          show-disabled
        >
          <div class="mt-4" :data-testid="`governance-level-body-${level.id}`">
            <output
              v-if="level.state.status === 'loading'"
              aria-live="polite"
              :aria-label="$t(level.titleKey)"
              class="block text-sm text-muted-foreground"
              :data-testid="`governance-level-loading-${level.id}`"
            >
              {{ $t('views.GovernanceStackView.loading') }}
            </output>

            <output
              v-else-if="level.state.status === 'restricted'"
              aria-live="polite"
              :aria-label="$t(level.titleKey)"
              class="block rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm"
              :data-testid="`governance-level-restricted-${level.id}`"
            >
              <p class="font-medium text-warning-text">{{ $t('views.GovernanceStackView.restricted_title') }}</p>
              <p class="mt-1 text-muted-foreground">
                {{ $t('views.GovernanceStackView.restricted_body', { gate: $t(level.gateKey) }) }}
              </p>
            </output>

            <div
              v-else-if="level.state.status === 'error'"
              role="alert"
              aria-live="assertive"
              class="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm"
              :data-testid="`governance-level-error-${level.id}`"
            >
              <p class="font-medium">{{ $t('views.GovernanceStackView.error_title') }}</p>
              <p class="mt-1 text-muted-foreground">{{ level.state.errorMessage }}</p>
              <button
                type="button"
                class="mt-2 rounded-md border border-border px-2 py-1 text-xs hover:bg-accent"
                :data-testid="`governance-level-retry-${level.id}`"
                @click="loadLevel(level)"
              >
                {{ $t('views.GovernanceStackView.retry') }}
              </button>
            </div>

            <div v-else :data-testid="`governance-level-ready-${level.id}`">
              <output
                class="flex flex-wrap items-center gap-3"
                aria-live="polite"
                :aria-label="$t(level.titleKey)"
              >
                <Badge
                  :severity="level.state.configured ? 'success' : 'secondary'"
                  :data-testid="`governance-level-state-${level.id}`"
                >
                  {{ level.state.configured ? $t('views.GovernanceStackView.state_configured') : $t('views.GovernanceStackView.state_empty') }}
                </Badge>
                <span class="text-sm font-medium" :data-testid="`governance-level-count-${level.id}`">
                  {{ $t(level.countLabelKey, { count: level.state.count }) }}
                </span>
              </output>
              <p
                v-if="level.state.detail"
                class="mt-2 text-xs text-muted-foreground"
                :data-testid="`governance-level-detail-${level.id}`"
              >
                {{ level.state.detail }}
              </p>
              <div class="mt-3 flex flex-wrap gap-3">
                <router-link
                  v-for="(link, index) in level.links"
                  :key="link.to"
                  :to="link.to"
                  class="text-sm font-medium text-primary hover:underline"
                  :data-testid="`governance-level-link-${level.id}-${index}`"
                >
                  {{ $t(link.labelKey) }}
                </router-link>
              </div>
            </div>
          </div>
        </FeatureGate>
      </article>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import PageHeader from '../components/shared/PageHeader.vue'
import FeatureGate from '../components/FeatureGate.vue'
import Badge from 'primevue/badge'

const { t } = useI18n()

type LevelStatus = 'loading' | 'ready' | 'restricted' | 'error'

interface LevelState {
  status: LevelStatus
  count: number
  configured: boolean
  detail: string | null
  errorMessage: string | null
}

interface LevelLink {
  to: string
  labelKey: string
}

interface LevelDef {
  id: string
  order: number
  titleKey: string
  primitiveKey: string
  countLabelKey: string
  noteKey?: string
  gateKey: string
  featureName?: string
  requiredTier: string
  links: LevelLink[]
}

interface LevelView extends LevelDef {
  state: LevelState
}

/**
 * The seven governance levels, mapped to Modulo's real primitives (per the
 * FAR-1696(a) spec §1). Each level is backed by an entity + list API that
 * already exists; this page composes them client-side (presentation-only, no
 * new backend endpoint).
 */
const LEVEL_DEFS: LevelDef[] = [
  {
    id: 'models',
    order: 1,
    titleKey: 'views.GovernanceStackView.level_models_title',
    primitiveKey: 'views.GovernanceStackView.level_models_primitive',
    countLabelKey: 'views.GovernanceStackView.level_models_count',
    gateKey: 'views.GovernanceStackView.gate_models',
    featureName: 'model_backend_management',
    requiredTier: 'community',
    links: [{ to: '/admin/model-backends', labelKey: 'views.GovernanceStackView.level_models_link' }],
  },
  {
    id: 'tools',
    order: 2,
    titleKey: 'views.GovernanceStackView.level_tools_title',
    primitiveKey: 'views.GovernanceStackView.level_tools_primitive',
    countLabelKey: 'views.GovernanceStackView.level_tools_count',
    gateKey: 'views.GovernanceStackView.gate_tools',
    featureName: 'plugin_management',
    requiredTier: 'community',
    links: [{ to: '/admin/connectors', labelKey: 'views.GovernanceStackView.level_tools_link' }],
  },
  {
    id: 'mcp',
    order: 3,
    titleKey: 'views.GovernanceStackView.level_mcp_title',
    primitiveKey: 'views.GovernanceStackView.level_mcp_primitive',
    countLabelKey: 'views.GovernanceStackView.level_mcp_count',
    noteKey: 'views.GovernanceStackView.level_mcp_note',
    gateKey: 'views.GovernanceStackView.gate_mcp',
    featureName: 'mcp_server',
    requiredTier: 'community',
    links: [{ to: '/settings/mcp', labelKey: 'views.GovernanceStackView.level_mcp_link' }],
  },
  {
    id: 'agents',
    order: 4,
    titleKey: 'views.GovernanceStackView.level_agents_title',
    primitiveKey: 'views.GovernanceStackView.level_agents_primitive',
    countLabelKey: 'views.GovernanceStackView.level_agents_count',
    noteKey: 'views.GovernanceStackView.level_agents_note',
    gateKey: 'views.GovernanceStackView.gate_agents',
    requiredTier: 'community',
    links: [{ to: '/library', labelKey: 'views.GovernanceStackView.level_agents_link' }],
  },
  {
    id: 'commands',
    order: 5,
    titleKey: 'views.GovernanceStackView.level_commands_title',
    primitiveKey: 'views.GovernanceStackView.level_commands_primitive',
    countLabelKey: 'views.GovernanceStackView.level_commands_count',
    noteKey: 'views.GovernanceStackView.level_commands_note',
    gateKey: 'views.GovernanceStackView.gate_commands',
    requiredTier: 'community',
    links: [{ to: '/library', labelKey: 'views.GovernanceStackView.level_commands_link' }],
  },
  {
    id: 'autonomy',
    order: 6,
    titleKey: 'views.GovernanceStackView.level_autonomy_title',
    primitiveKey: 'views.GovernanceStackView.level_autonomy_primitive',
    countLabelKey: 'views.GovernanceStackView.level_autonomy_count',
    gateKey: 'views.GovernanceStackView.gate_autonomy',
    requiredTier: 'community',
    links: [{ to: '/pipelines', labelKey: 'views.GovernanceStackView.level_autonomy_link' }],
  },
  {
    id: 'telemetry',
    order: 7,
    titleKey: 'views.GovernanceStackView.level_telemetry_title',
    primitiveKey: 'views.GovernanceStackView.level_telemetry_primitive',
    countLabelKey: 'views.GovernanceStackView.level_telemetry_count',
    gateKey: 'views.GovernanceStackView.gate_telemetry',
    featureName: 'observability',
    requiredTier: 'team',
    links: [
      { to: '/settings/observability', labelKey: 'views.GovernanceStackView.level_telemetry_link' },
      { to: '/admin/errors', labelKey: 'views.GovernanceStackView.level_telemetry_link_errors' },
      { to: '/admin/notification-delivery', labelKey: 'views.GovernanceStackView.level_telemetry_link_notifications' },
    ],
  },
]

const levels = reactive<LevelView[]>(
  LEVEL_DEFS.map((def) => ({
    ...def,
    state: { status: 'loading', count: 0, configured: false, detail: null, errorMessage: null },
  })),
)

/** A request that REJECTS (network failure, abort) never reaches the typed `{ data, error }` shape. */
function rejected(e: unknown): { data?: undefined; error?: { detail?: unknown }; response?: undefined } {
  return { error: { detail: formatApiError(e) } }
}

interface ApiResultLike {
  error?: unknown
  response?: { status?: number }
}

/**
 * A list call can come back 402 (feature gated) or 403 (permission) for a
 * viewer. Both are a RESTRICTED STATE for that level only - never a page
 * failure. The api client normalises error bodies to a ProblemDetail carrying
 * `status`; the raw HTTP status is on `response`.
 */
function isRestricted(resp: ApiResultLike | null | undefined): boolean {
  const status = resp?.response?.status
  if (status === 402 || status === 403) return true
  const err = resp?.error
  if (err && typeof err === 'object') {
    const errorStatus = (err as { status?: unknown }).status
    if (errorStatus === 402 || errorStatus === 403) return true
  }
  return false
}

type Outcome =
  | { kind: 'ready'; count: number; configured: boolean; detail: string | null }
  | { kind: 'restricted' }
  | { kind: 'error'; message: string }

async function loadModels(): Promise<Outcome> {
  const resp = await api.GET('/api/v1/model-backends', { params: { query: { page_size: 1 } } }).catch(rejected)
  if (isRestricted(resp)) return { kind: 'restricted' }
  if (resp.error) return { kind: 'error', message: formatApiError(resp.error) }
  const count = resp.data?.total ?? 0
  return { kind: 'ready', count, configured: count > 0, detail: null }
}

async function loadTools(): Promise<Outcome> {
  const resp = await api.GET('/api/v1/connectors', { params: { query: { page_size: 1 } } }).catch(rejected)
  if (isRestricted(resp)) return { kind: 'restricted' }
  if (resp.error) return { kind: 'error', message: formatApiError(resp.error) }
  const count = resp.data?.total ?? 0
  return { kind: 'ready', count, configured: count > 0, detail: null }
}

async function loadMcp(): Promise<Outcome> {
  const [clientsResp, keysResp] = await Promise.all([
    api.GET('/api/v1/mcp/oauth/clients').catch(rejected),
    api.GET('/api/v1/api-keys').catch(rejected),
  ])
  if (isRestricted(clientsResp) || isRestricted(keysResp)) return { kind: 'restricted' }
  if (clientsResp.error) return { kind: 'error', message: formatApiError(clientsResp.error) }
  if (keysResp.error) return { kind: 'error', message: formatApiError(keysResp.error) }
  const clients = Array.isArray(clientsResp.data) ? clientsResp.data.length : 0
  const keys = Array.isArray(keysResp.data) ? keysResp.data.length : 0
  const count = clients + keys
  return {
    kind: 'ready',
    count,
    configured: count > 0,
    detail: t('views.GovernanceStackView.mcp_detail', { clients, keys }),
  }
}

async function loadAgents(): Promise<Outcome> {
  const resp = await api.GET('/api/v1/agents', { params: { query: { page: 1, page_size: 1 } } }).catch(rejected)
  if (isRestricted(resp)) return { kind: 'restricted' }
  if (resp.error) return { kind: 'error', message: formatApiError(resp.error) }
  const count = resp.data?.total ?? 0
  return { kind: 'ready', count, configured: count > 0, detail: null }
}

async function loadCommands(): Promise<Outcome> {
  const [compositesResp, librariesResp] = await Promise.all([
    api.GET('/api/v1/composite-templates', { params: { query: { page_size: 1 } } }).catch(rejected),
    api.GET('/api/v1/libraries', { params: { query: { page_size: 1 } } }).catch(rejected),
  ])
  if (isRestricted(compositesResp) || isRestricted(librariesResp)) return { kind: 'restricted' }
  if (compositesResp.error) return { kind: 'error', message: formatApiError(compositesResp.error) }
  if (librariesResp.error) return { kind: 'error', message: formatApiError(librariesResp.error) }
  const composites = compositesResp.data?.total ?? 0
  const primitives = librariesResp.data?.total ?? 0
  const count = composites + primitives
  return {
    kind: 'ready',
    count,
    configured: count > 0,
    detail: t('views.GovernanceStackView.commands_detail', { composites, primitives }),
  }
}

/**
 * The rung distribution has to describe EVERY pipeline, not just the first
 * page. `page_size` is capped at 100 server-side while the level's count comes
 * from `total`, so a single-page read made the ladder disagree with the count
 * it sits under (150 pipelines above a distribution that sums to 100). Walk
 * the keyset cursor the list endpoint already returns, bounded so a very large
 * org cannot turn a readout into an unbounded crawl; when the bound truncates
 * the walk, the detail says it is describing a prefix rather than presenting a
 * partial tally as the whole ladder.
 */
const AUTONOMY_PAGE_SIZE = 100
const AUTONOMY_MAX_PAGES = 20

async function loadAutonomy(): Promise<Outcome> {
  let manual = 0
  let notify = 0
  let autonomous = 0
  let total = 0
  let sampled = 0
  let truncated = false
  let walked = false
  let cursor: string | undefined
  for (let page = 1; page <= AUTONOMY_MAX_PAGES; page += 1) {
    const query = cursor ? { page_size: AUTONOMY_PAGE_SIZE, cursor } : { page_size: AUTONOMY_PAGE_SIZE }
    const resp = await api.GET('/api/v1/pipelines', { params: { query } }).catch(rejected)
    if (isRestricted(resp)) return { kind: 'restricted' }
    if (resp.error) return { kind: 'error', message: formatApiError(resp.error) }
    const data = resp.data
    total = data?.total ?? total
    for (const pipeline of data?.items ?? []) {
      // The earned level is NULL until a runtime promote/demote sets it; the
      // pinned default is the effective rung until then.
      const level = pipeline.earned_autonomy_level ?? pipeline.default_autonomy_level ?? 'manual_approval'
      if (level === 'notify_on_complete') notify += 1
      else if (level === 'fully_autonomous') autonomous += 1
      else manual += 1
      sampled += 1
    }
    const next = data?.next_cursor
    if (!data?.has_more || !next) {
      // `has_more` with no cursor is a continuation we cannot follow; say so
      // rather than reporting the prefix as the whole ladder.
      truncated = Boolean(data?.has_more)
      walked = true
      break
    }
    cursor = next
  }
  // Exhausting the page budget with pages still available is the other way to
  // hold a prefix; both mean the ladder is not the whole story.
  if (!walked) truncated = true
  const rungs = { manual, notify, autonomous }
  return {
    kind: 'ready',
    count: total,
    configured: total > 0,
    detail: truncated
      ? t('views.GovernanceStackView.autonomy_detail_partial', { ...rungs, sampled, total })
      : t('views.GovernanceStackView.autonomy_detail', rungs),
  }
}

async function loadTelemetry(): Promise<Outcome> {
  // OTel export config owns the level's restricted/error state; the error
  // dashboard and notification endpoints are best-effort enrichment, so a
  // 402/403/transport failure on either degrades to "unavailable" rather than
  // taking the whole telemetry level down.
  const [otelResp, errorsResp, endpointsResp] = await Promise.all([
    api.GET('/api/v1/settings/observability').catch(rejected),
    api.GET('/api/v1/errors', { params: { query: { limit: 1 } } }).catch(rejected),
    api.GET('/api/v1/notifications').catch(rejected),
  ])
  if (isRestricted(otelResp)) return { kind: 'restricted' }
  if (otelResp.error) return { kind: 'error', message: formatApiError(otelResp.error) }
  const otelConfigured = Boolean(otelResp.data?.effective_otlp_endpoint)
  const errorGroups =
    !errorsResp.error && !isRestricted(errorsResp) ? (errorsResp.data?.total ?? 0) : null
  const endpoints =
    !endpointsResp.error && !isRestricted(endpointsResp) && Array.isArray(endpointsResp.data)
      ? endpointsResp.data.length
      : null
  const count = (errorGroups ?? 0) + (endpoints ?? 0)
  const unavailable = t('views.GovernanceStackView.telemetry_unavailable')
  return {
    kind: 'ready',
    count,
    configured: otelConfigured || count > 0,
    detail: t('views.GovernanceStackView.telemetry_detail', {
      otel: otelConfigured
        ? t('views.GovernanceStackView.telemetry_otel_configured')
        : t('views.GovernanceStackView.telemetry_otel_not_configured'),
      errors: errorGroups === null ? unavailable : String(errorGroups),
      endpoints: endpoints === null ? unavailable : String(endpoints),
    }),
  }
}

const LOADERS: Record<string, () => Promise<Outcome>> = {
  models: loadModels,
  tools: loadTools,
  mcp: loadMcp,
  agents: loadAgents,
  commands: loadCommands,
  autonomy: loadAutonomy,
  telemetry: loadTelemetry,
}

function applyOutcome(level: LevelView, outcome: Outcome): void {
  if (outcome.kind === 'ready') {
    level.state.status = 'ready'
    level.state.count = outcome.count
    level.state.configured = outcome.configured
    level.state.detail = outcome.detail
  } else if (outcome.kind === 'restricted') {
    level.state.status = 'restricted'
    level.state.detail = null
  } else {
    level.state.status = 'error'
    level.state.errorMessage = outcome.message
  }
}

async function loadLevel(level: LevelView): Promise<void> {
  level.state.status = 'loading'
  level.state.errorMessage = null
  try {
    applyOutcome(level, await LOADERS[level.id]())
  } catch (e: unknown) {
    level.state.status = 'error'
    level.state.errorMessage = formatApiError(e)
  }
}

async function loadAll(): Promise<void> {
  await Promise.all(levels.map((level) => loadLevel(level)))
}

onMounted(() => {
  void loadAll()
})
</script>
