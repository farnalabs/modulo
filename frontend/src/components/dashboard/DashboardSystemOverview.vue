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

    <SystemMapDraftReview />
  </SectionCard>
</template>

<script setup lang="ts">
import SectionCard from '../shared/SectionCard.vue'
import SystemMapDraftReview from '../system-maps/SystemMapDraftReview.vue'
import { useSystemMapsStore } from '../../stores/systemMaps'

const store = useSystemMapsStore()
void store.fetchDiscovery()
</script>
