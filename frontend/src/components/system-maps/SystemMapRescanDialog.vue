<template>
  <div class="fixed inset-0 z-50" :data-testid="dataTestId">
    <div class="absolute inset-0 bg-black/50" @click="$emit('close')" aria-hidden="true" />
    <section class="absolute left-1/2 top-1/2 w-full max-w-xl -translate-x-1/2 -translate-y-1/2 rounded-lg bg-card shadow-2xl" role="dialog" aria-modal="true" aria-labelledby="rescan-title">
      <header class="border-b px-4 py-3"><h2 id="rescan-title" class="font-semibold">{{ $t('views.SystemMapDetailView.rescan_title') }}</h2></header>
      <div class="p-4">
        <p class="text-sm text-muted-foreground mb-4">{{ $t('views.SystemMapDetailView.rescan_hint') }}</p>
        <div v-if="loading" class="py-8 text-center text-sm text-muted-foreground" aria-live="polite">{{ $t('views.SystemMapDetailView.scanning') }}</div>
        <div v-else-if="diff" class="space-y-3" aria-live="polite">
          <DiffRow :label="$t('views.SystemMapDetailView.added_nodes')" :items="diff.added_nodes" />
          <DiffRow :label="$t('views.SystemMapDetailView.removed_nodes')" :items="diff.removed_nodes" />
          <DiffRow :label="$t('views.SystemMapDetailView.added_relationships')" :items="diff.added_relationships" />
          <DiffRow :label="$t('views.SystemMapDetailView.removed_relationships')" :items="diff.removed_relationships" />
          <DiffRow :label="$t('views.SystemMapDetailView.ambiguous_matches')" :items="diff.ambiguous_matches" tone="warning" />
          <p class="rounded bg-muted p-3 text-xs text-muted-foreground">{{ $t('views.SystemMapDetailView.rescan_preserves_edits') }}</p>
        </div>
      </div>
      <footer class="border-t px-4 py-3 flex justify-end gap-2">
        <button type="button" class="rounded border px-3 py-1.5 text-sm" @click="$emit('close')" data-testid="system-map-rescan-cancel">{{ $t('common.cancel') }}</button>
        <button type="button" class="rounded bg-primary px-3 py-1.5 text-sm text-primary-foreground" :disabled="loading" @click="$emit('apply')" data-testid="system-map-rescan-apply">{{ $t('views.SystemMapDetailView.apply_rescan') }}</button>
      </footer>
    </section>
  </div>
</template>
<script setup lang="ts">
import DiffRow from './SystemMapDiffRow.vue'
defineProps<{ diff: { added_nodes?: unknown[]; removed_nodes?: unknown[]; added_relationships?: unknown[]; removed_relationships?: unknown[]; ambiguous_matches?: unknown[] } | null; loading: boolean; dataTestId?: string }>()
defineEmits<{ (e: 'close'): void; (e: 'apply'): void }>()
</script>
