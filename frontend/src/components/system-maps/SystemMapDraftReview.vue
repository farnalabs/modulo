<template>
  <section class="rounded-md border border-dashed border-border bg-muted/30 p-3" data-testid="system-map-draft-review" aria-live="polite">
    <h3 class="font-medium">{{ $t('views.DashboardView.system_maps_review_title') }}</h3>
    <p v-if="store.discovering" class="mt-1 text-sm text-muted-foreground" data-testid="system-map-discovery-loading">
      {{ $t('views.DashboardView.system_maps_discovering') }}
    </p>
    <p v-else-if="store.discoveryError" class="mt-1 text-sm text-destructive" role="alert" data-testid="system-map-discovery-error">
      {{ $t('views.DashboardView.system_maps_discovery_error') }}
    </p>
    <template v-else-if="store.discoveryReport">
      <p class="mt-1 text-sm" data-testid="system-map-discovery-report">
        {{
          $t('views.DashboardView.system_maps_discovery_summary', {
            nodes: store.discoveryReport?.node_count ?? 0,
            edges: store.discoveryReport?.edge_count ?? 0,
            sources: store.discoveryReport?.sources_inspected.length ?? 0,
            revision: store.discoveryReport?.draft_revision_number ?? 0,
          })
        }}
      </p>
      <p v-if="store.discoveryReport?.warnings.length" class="mt-2 text-xs text-muted-foreground">
        {{ $t('views.DashboardView.system_maps_discovery_warnings', { count: store.discoveryReport?.warnings.length ?? 0 }) }}
      </p>
      <p v-if="store.discoveryReport?.skipped.length" class="mt-2 text-xs text-muted-foreground">
        {{ $t('views.DashboardView.system_maps_discovery_skipped', { count: store.discoveryReport?.skipped.length ?? 0 }) }}
      </p>
      <p class="mt-2 text-xs text-muted-foreground">{{ $t('views.DashboardView.system_maps_no_publish_note') }}</p>
    </template>
    <template v-else>
      <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.DashboardView.system_maps_coming_soon') }}</p>
      <p class="mt-2 text-xs text-muted-foreground">{{ $t('views.DashboardView.system_maps_no_publish_note') }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { useSystemMapsStore } from '../../stores/systemMaps'

const store = useSystemMapsStore()
</script>
