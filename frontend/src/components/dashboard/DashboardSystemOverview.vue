<template>
  <SectionCard
    :title="$t('views.DashboardView.system_overview')"
    :description="$t('views.DashboardView.system_overview_description')"
    data-testid="dashboard-system-overview"
  >
    <template #header>
      <button
        type="button"
        :disabled="discovering"
        class="rounded-md border px-3 py-2 text-sm font-medium disabled:opacity-60"
        data-testid="dashboard-system-discover"
        :aria-label="$t('views.DashboardView.discover_my_system')"
        @click="discoverSystem"
      >
        {{ $t('views.DashboardView.discover_my_system') }}
      </button>
    </template>

    <ErrorAlert
      v-if="discoverError"
      :message="discoverError"
      :on-dismiss="clearDiscoverError"
      :dismiss-label="$t('common.dismiss')"
    />
    <div v-if="store.overviewLoading" class="text-sm text-muted-foreground" data-testid="system-map-overview-loading">
      {{ $t('views.DashboardView.system_maps_loading') }}
    </div>
    <ErrorAlert
      v-else-if="store.overviewError"
      :message="$t('views.DashboardView.system_maps_error')"
      :on-retry="loadOverview"
      aria-live="assertive"
      data-testid="system-map-overview-error"
    />
    <EmptyState
      v-else-if="store.overviewEmpty"
      :title="$t('views.DashboardView.system_maps_empty_title')"
      :description="$t('views.DashboardView.system_maps_empty_description')"
      data-testid="system-map-overview-empty"
    />
    <div v-else-if="store.overview" class="space-y-3" data-testid="system-map-overview-metrics">
      <p class="text-xs text-muted-foreground">
        {{ $t('views.DashboardView.system_maps_window', { days: store.overview.window_days }) }}
      </p>
      <div v-for="node in store.overview.nodes" :key="node.node_key" class="rounded-md border p-3" data-testid="system-map-node">
        <div class="flex items-center justify-between gap-2">
          <span class="font-medium">{{ node.display_name }}</span>
          <span class="text-xs text-muted-foreground">{{ healthLabel(node.health) }}</span>
        </div>
        <div class="mt-2 grid grid-cols-2 gap-2 text-xs text-muted-foreground sm:grid-cols-4">
          <span>{{ $t('views.DashboardView.system_maps_throughput') }}: {{ node.throughput_per_min ?? $t('common.na') }}</span>
          <span>{{ $t('views.DashboardView.system_maps_failure_rate') }}: {{ node.failure_rate == null ? $t('common.na') : `${Math.round(node.failure_rate * 100)}%` }}</span>
          <span>{{ $t('views.DashboardView.system_maps_queue') }}: {{ node.queue_depth ?? $t('common.na') }}</span>
          <span>{{ $t('views.DashboardView.system_maps_telemetry') }}: {{ node.telemetry_coverage == null ? $t('common.na') : `${Math.round(node.telemetry_coverage * 100)}%` }}</span>
        </div>
      </div>
    </div>
    <SystemMapDraftReview v-else />
  </SectionCard>
</template>

<script setup lang="ts">
import SectionCard from '../shared/SectionCard.vue'
import EmptyState from '../shared/EmptyState.vue'
import ErrorAlert from '../shared/ErrorAlert.vue'
import SystemMapDraftReview from '../system-maps/SystemMapDraftReview.vue'
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { useSystemMapsStore } from '../../stores/systemMaps'

const { t } = useI18n()
const router = useRouter()
const store = useSystemMapsStore()
const discovering = ref(false)
const discoverError = ref<string | null>(null)
const healthLabel = (health: string): string => t(`views.DashboardView.system_maps_health_${health}`)

function clearDiscoverError(): void { discoverError.value = null }

/**
 * Discovery persists an unpublished draft, so hand the user straight to the
 * draft's detail view with the review panel open rather than leaving the
 * dashboard preview as the only outcome.
 */
async function discoverSystem(): Promise<void> {
  if (discovering.value) return
  discovering.value = true
  discoverError.value = null
  try {
    const report = await store.discover()
    await router.push(`/system-maps/${report.draft_map_id}?discovery=1`)
  } catch (err: unknown) {
    discoverError.value = err instanceof Error ? err.message : t('views.DashboardView.system_maps_discover_failed')
  } finally {
    discovering.value = false
  }
}

/**
 * Load every source this overview renders from. The retry affordance reuses
 * this same entry point, so a retry re-runs the whole load rather than only the
 * leaf overview request.
 */
async function loadOverview(): Promise<void> {
  await store.fetchDiscovery()
  await store.fetchDefaultOverview()
}

onMounted(loadOverview)
</script>
