<template>
  <SectionCard
    :title="$t('views.DashboardView.system_overview')"
    :description="$t('views.DashboardView.system_overview_description')"
    data-testid="dashboard-system-overview"
  >
    <template #header>
      <button
        v-if="canDiscover"
        type="button"
        :disabled="discovering"
        class="rounded-md border px-3 py-2 text-sm font-medium disabled:opacity-60"
        :aria-label="$t('views.DashboardView.discover_my_system')"
        data-testid="dashboard-system-discover"
        @click="discoverSystem"
      >
        {{ $t('views.DashboardView.discover_my_system') }}
      </button>
      <button
        type="button"
        class="rounded p-1 text-muted-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        data-testid="system-map-overview-toggle"
        :aria-expanded="!collapsed"
        aria-controls="system-map-overview-content"
        :aria-label="$t(collapsed ? 'views.DashboardView.expand_system_overview' : 'views.DashboardView.collapse_system_overview')"
        @click="collapsed = !collapsed"
      >
        <ChevronDown class="h-4 w-4 transition-transform" :class="{ 'rotate-180': !collapsed }" aria-hidden="true" />
      </button>
    </template>

    <ErrorAlert
      v-if="discoverError"
      :message="discoverError"
      :on-dismiss="clearDiscoverError"
      :dismiss-label="$t('common.dismiss')"
      class="mb-3"
      data-testid="dashboard-system-discover-error"
    />
    <div v-if="collapsed" id="system-map-overview-content" data-testid="system-map-overview-content" class="text-sm text-muted-foreground">
      {{ $t('views.DashboardView.system_overview_collapsed') }}
    </div>
    <div v-else id="system-map-overview-content">
      <div v-if="store.overviewLoading" class="text-sm text-muted-foreground" data-testid="system-map-overview-loading">
        {{ $t('views.DashboardView.system_maps_loading') }}
      </div>
      <!-- 402 feature_required: unavailable, not a failed fetch - no retry. -->
      <ErrorAlert
        v-else-if="store.overviewUnavailable"
        :message="$t('views.DashboardView.system_maps_unavailable')"
        aria-live="polite"
        data-testid="system-map-overview-error"
      />
      <ErrorAlert
        v-else-if="store.overviewError"
        :message="$t('views.DashboardView.system_maps_error')"
        :on-retry="loadOverview"
        aria-live="assertive"
        data-testid="system-map-overview-error"
      />
      <EmptyState
        v-else-if="store.overviewEmpty || !hasObservedNodes"
        :title="$t(store.overviewEmpty ? 'views.DashboardView.system_maps_empty_title' : 'views.DashboardView.system_maps_no_observed_title')"
        :description="$t(store.overviewEmpty ? 'views.DashboardView.system_maps_empty_description' : 'views.DashboardView.system_maps_no_observed_description')"
        data-testid="system-map-overview-empty"
      />
      <div v-else-if="store.overview" class="space-y-3" data-testid="system-map-overview-metrics">
        <p class="text-xs text-muted-foreground">
          {{ $t('views.DashboardView.system_maps_window', { days: props.windowDays }) }}
        </p>
        <div v-for="node in observedNodes" :key="node.node_key" class="rounded-md border p-3" data-testid="system-map-node">
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
    </div>
  </SectionCard>
</template>

<script setup lang="ts">
import SectionCard from '../shared/SectionCard.vue'
import EmptyState from '../shared/EmptyState.vue'
import ErrorAlert from '../shared/ErrorAlert.vue'
import SystemMapDraftReview from '../system-maps/SystemMapDraftReview.vue'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useStorage } from '@vueuse/core'
import { ChevronDown } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { useSystemMapsStore } from '../../stores/systemMaps'

const props = withDefaults(defineProps<{ windowDays?: number }>(), { windowDays: 7 })
const { t } = useI18n()
const router = useRouter()
const store = useSystemMapsStore()
const discovering = ref(false)
const discoverError = ref<string | null>(null)
const canDiscover = computed(() => store.accessLoaded && !store.error && store.hasAccess)
const collapsed = useStorage('dashboard-system-overview-collapsed', false)
const observedNodes = computed(() => store.overview?.nodes.filter((node) =>
  node.health !== 'unknown' || node.queue_depth != null || node.throughput_per_min != null || node.failure_rate != null || node.telemetry_coverage != null,
) ?? [])
const hasObservedNodes = computed(() => observedNodes.value.length > 0)
const healthLabel = (health: string): string => t(`views.DashboardView.system_maps_health_${health}`)

function clearDiscoverError(): void {
  discoverError.value = null
}

/**
 * Discovery can fail (network, authorization, server error). Surface that
 * inline rather than letting the rejection escape the click handler and
 * leaving the card silent with a re-enabled button.
 */
async function discoverSystem(): Promise<void> {
  if (discovering.value) return
  discovering.value = true
  discoverError.value = null
  try {
    const report = await store.discover()
    await router.push(`/system-maps/${report.draft_map_id}?discovery=1`)
  } catch {
    discoverError.value = t('views.DashboardView.system_maps_discover_failed')
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
  await store.fetchDefaultOverview(props.windowDays)
}

onMounted(loadOverview)
</script>
