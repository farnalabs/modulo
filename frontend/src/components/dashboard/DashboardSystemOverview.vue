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
        disabled
        class="rounded-md border px-3 py-2 text-sm font-medium opacity-60"
        data-testid="dashboard-system-discover"
        :aria-label="$t('views.DashboardView.discover_my_system')"
      >
        {{ $t('views.DashboardView.discover_my_system') }}
      </button>
    </template>

    <div v-if="store.overviewLoading" class="text-sm text-muted-foreground" data-testid="system-map-overview-loading">
      {{ $t('views.DashboardView.system_maps_loading') }}
    </div>
    <div v-else-if="store.overviewError" class="text-sm text-destructive" data-testid="system-map-overview-error" aria-live="assertive">
      {{ $t('views.DashboardView.system_maps_error') }}
    </div>
    <div v-else-if="store.overview" class="space-y-3" data-testid="system-map-overview-metrics">
      <p class="text-xs text-muted-foreground">
        {{ $t('views.DashboardView.system_maps_window') }}
      </p>
      <div v-for="node in store.overview.nodes" :key="node.node_key" class="rounded-md border p-3" data-testid="system-map-node">
        <div class="flex items-center justify-between gap-2">
          <span class="font-medium">{{ node.display_name }}</span>
          <span class="text-xs text-muted-foreground">{{ node.health }}</span>
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
import SystemMapDraftReview from '../system-maps/SystemMapDraftReview.vue'
import { onMounted } from 'vue'
import { useSystemMapsStore } from '../../stores/systemMaps'

const store = useSystemMapsStore()

onMounted(async () => {
  await store.fetchDiscovery()
  await store.fetchDefaultOverview()
})
</script>
