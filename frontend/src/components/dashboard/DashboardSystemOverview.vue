<template>
  <section class="card p-4" data-testid="dashboard-system-overview" aria-live="polite">
    <div class="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-base font-semibold">{{ $t('views.DashboardView.system_overview') }}</h2>
        <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.DashboardView.system_overview_description') }}</p>
      </div>
      <button
        type="button"
        disabled
        class="rounded-md border px-3 py-2 text-sm font-medium opacity-60"
        data-testid="dashboard-system-discover"
        :aria-label="$t('views.DashboardView.discover_my_system')"
      >
        {{ $t('views.DashboardView.discover_my_system') }}
      </button>
    </div>

    <SystemMapDraftReview />
  </section>
</template>

<script setup lang="ts">
import SystemMapDraftReview from '../system-maps/SystemMapDraftReview.vue'
import { useSystemMapsStore } from '../../stores/systemMaps'

const store = useSystemMapsStore()
void store.fetchDiscovery()
</script>
