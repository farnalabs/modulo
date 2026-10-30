<template>
  <section class="card p-4" data-testid="dashboard-system-overview" aria-live="polite">
    <div class="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-base font-semibold">{{ $t('views.DashboardView.system_overview') }}</h2>
        <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.DashboardView.system_overview_description') }}</p>
      </div>
      <button type="button" disabled class="rounded-md border px-3 py-2 text-sm font-medium opacity-60" data-testid="dashboard-system-discover">
        {{ $t('views.DashboardView.discover_my_system') }}
      </button>
    </div>

    <div v-if="store.loading" class="space-y-3" data-testid="dashboard-system-loading">
      <div v-for="item in 3" :key="item" class="h-12 animate-pulse rounded bg-muted" />
    </div>
    <ErrorAlert v-else-if="store.error" :message="store.error" :on-retry="store.fetchOverview" />
    <EmptyState v-else-if="!store.maps.length" :title="$t('views.DashboardView.system_overview_empty_title')" :description="$t('views.DashboardView.system_overview_empty_description')" data-testid="dashboard-system-empty" />
    <EmptyState v-else-if="!store.hasPublishedMap || !store.overview" :title="$t('views.DashboardView.system_overview_unpublished_title')" :description="$t('views.DashboardView.system_overview_unpublished_description')" data-testid="dashboard-system-unpublished" />
    <template v-else>
      <div class="mb-4 rounded-md bg-muted/40 p-3" data-testid="dashboard-system-map-meta">
        <p class="font-medium">{{ store.selectedMap?.name }}</p>
        <p class="text-sm text-muted-foreground">{{ $t('views.DashboardView.system_revision', { revision: store.overview.revision_number }) }}</p>
      </div>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" data-testid="dashboard-system-node-health">
        <article v-for="node in store.overview.nodes" :key="node.node_key" class="rounded-md border p-3">
          <div class="flex items-center justify-between gap-2">
            <h3 class="font-medium">{{ node.display_name }}</h3>
            <span class="text-xs font-medium capitalize" :class="healthClass(node.health)">{{ healthLabel(node.health) }}</span>
          </div>
          <dl class="mt-3 grid grid-cols-2 gap-2 text-xs text-muted-foreground">
            <div><dt>{{ $t('views.DashboardView.queue_depth') }}</dt><dd class="font-medium text-foreground">{{ metric(node.queue_depth) }}</dd></div>
            <div><dt>{{ $t('views.DashboardView.throughput') }}</dt><dd class="font-medium text-foreground">{{ metric(node.throughput_per_min) }}</dd></div>
            <div><dt>{{ $t('views.DashboardView.failure_rate') }}</dt><dd class="font-medium text-foreground">{{ percent(node.failure_rate) }}</dd></div>
            <div><dt>{{ $t('views.DashboardView.telemetry_coverage') }}</dt><dd class="font-medium text-foreground">{{ percent(node.telemetry_coverage) }}</dd></div>
          </dl>
        </article>
      </div>
      <div v-if="store.overview.loop_configs.length" class="mt-4" data-testid="dashboard-system-loops">
        <h3 class="mb-2 text-sm font-semibold">{{ $t('views.DashboardView.system_loops') }}</h3>
        <ul class="space-y-2 text-sm">
          <li v-for="loop in store.overview.loop_configs" :key="loop.node_key" class="flex flex-wrap justify-between gap-2 rounded border p-2">
            <span>{{ loop.node_key }} · {{ loop.target_signal }}</span>
            <span class="text-muted-foreground">{{ loop.cooldown_active ? $t('views.DashboardView.cooldown_active') : $t('views.DashboardView.cooldown_inactive') }}</span>
          </li>
        </ul>
      </div>
      <div v-if="store.overview.bottlenecks.length" class="mt-4" data-testid="dashboard-system-bottlenecks">
        <h3 class="mb-2 text-sm font-semibold">{{ $t('views.DashboardView.bottlenecks') }}</h3>
        <ul class="space-y-2 text-sm"><li v-for="item in store.overview.bottlenecks" :key="item.node_key" class="rounded border p-2"><strong>{{ item.display_name }}</strong>: {{ item.detail }}</li></ul>
      </div>
    </template>
    <p class="mt-4 text-xs text-muted-foreground">{{ $t('views.DashboardView.discovery_beta_note') }}</p>
  </section>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import ErrorAlert from '../shared/ErrorAlert.vue'
import EmptyState from '../shared/EmptyState.vue'
import { useSystemMapsStore } from '../../stores/systemMaps'

const { t } = useI18n()
const store = useSystemMapsStore()
store.fetchOverview()

function metric(value: number | null | undefined): string | number { return value == null ? t('views.DashboardView.unknown_metric') : value }
function percent(value: number | null | undefined): string { return value == null ? t('views.DashboardView.unobserved_metric') : `${(value * 100).toFixed(1)}%` }
function healthLabel(value: string): string { return value === 'unknown' ? t('views.DashboardView.unknown_metric') : value }
function healthClass(value: string): string { return value === 'healthy' ? 'text-success-text' : value === 'unknown' ? 'text-muted-foreground' : 'text-destructive' }
</script>
