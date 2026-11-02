<template>
  <section class="rounded-md border border-dashed border-border bg-muted/30 p-3" data-testid="system-map-draft-review" aria-live="polite">
    <h3 class="font-medium">{{ t('views.DashboardView.system_maps_review_title') }}</h3>
    <p v-if="loading" class="mt-1 text-sm text-muted-foreground" data-testid="system-map-draft-loading">
      {{ t('views.DashboardView.system_maps_draft_loading') }}
    </p>
    <p v-else-if="error" class="mt-1 text-sm text-destructive" data-testid="system-map-draft-error">
      {{ t('views.DashboardView.system_maps_draft_error') }} {{ error }}
    </p>
    <template v-else-if="draft">
      <p class="mt-1 text-sm" data-testid="system-map-draft-summary">
        {{ t('views.DashboardView.system_maps_draft_summary', { nodes: draft.node_count, edges: draft.edge_count, sources: draft.sources_inspected.length }) }}
      </p>
      <p class="mt-1 text-xs text-muted-foreground" data-testid="system-map-draft-revision">
        {{ t('views.DashboardView.system_maps_draft_revision', { number: draft.draft_revision_number, id: draft.draft_revision_id }) }}
      </p>
      <p class="mt-1 text-xs text-muted-foreground" data-testid="system-map-draft-not-published">
        {{ t('views.DashboardView.system_maps_draft_not_published') }}
      </p>
      <div v-if="draft.warnings.length > 0" class="mt-2 text-xs" data-testid="system-map-draft-warnings">
        <span class="font-medium">{{ t('views.DashboardView.system_maps_draft_warnings') }}</span>
        <ul class="mt-1 list-disc pl-4 text-muted-foreground">
          <li v-for="(warning, index) in draft.warnings" :key="index">{{ warning }}</li>
        </ul>
      </div>
      <div v-if="draft.skipped.length > 0" class="mt-2 text-xs" data-testid="system-map-draft-skipped">
        <span class="font-medium">{{ t('views.DashboardView.system_maps_draft_skipped') }}</span>
        <ul class="mt-1 list-disc pl-4 text-muted-foreground">
          <li v-for="(item, index) in draft.skipped" :key="index">{{ item.source }}: {{ item.reason }}</li>
        </ul>
      </div>
    </template>
    <p v-else class="mt-2 text-xs text-muted-foreground" data-testid="system-map-draft-empty">
      {{ t('views.DashboardView.system_maps_draft_empty') }}
    </p>
  </section>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { components } from '../../lib/api/schema'

defineProps<{
  draft: components['schemas']['SystemMapDiscoveryReport'] | null
  loading: boolean
  error: string | null
}>()

const { t } = useI18n()
</script>
