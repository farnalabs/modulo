<template>
  <SectionCard
    :title="$t('views.DashboardView.system_overview')"
    :description="$t('views.DashboardView.system_overview_description')"
    data-testid="dashboard-system-overview"
    aria-live="polite"
  >
    <template #header>
      <button
        type="button"
        :disabled="store.discoveryLoading"
        class="rounded-md border px-3 py-2 text-sm font-medium disabled:opacity-60"
        data-testid="dashboard-system-discover"
        :aria-label="$t('views.DashboardView.discover_my_system')"
        @click="runDiscovery"
      >
        {{ store.discoveryLoading ? $t('views.DashboardView.discover_my_system_running') : $t('views.DashboardView.discover_my_system') }}
      </button>
    </template>

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
    <SystemMapDraftReview
      :draft="store.draft"
      :loading="store.discoveryLoading"
      :error="store.discoveryError"
    />
  </SectionCard>
</template>

<script setup lang="ts">
import SectionCard from '../shared/SectionCard.vue'
import EmptyState from '../shared/EmptyState.vue'
import ErrorAlert from '../shared/ErrorAlert.vue'
import SystemMapDraftReview from '../system-maps/SystemMapDraftReview.vue'
import { onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useSystemMapsStore } from '../../stores/systemMaps'

const { t } = useI18n()
const store = useSystemMapsStore()
const healthLabel = (health: string): string => t(`views.DashboardView.system_maps_health_${health}`)

/**
 * Load the overview the card renders. Discovery is deliberately NOT run here:
 * it is a mutating POST that persists a draft, so it waits for the explicit
 * "Discover My System" action. A retry re-runs only the overview load.
 */
async function loadOverview(): Promise<void> {
  await store.fetchDefaultOverview()
}

/** Run discovery from the operator's explicit action, then refresh the overview. */
async function runDiscovery(): Promise<void> {
  await store.fetchDiscovery()
  await store.fetchDefaultOverview()
}

onMounted(loadOverview)
</script>
