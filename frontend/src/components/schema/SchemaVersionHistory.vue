<template>
  <section class="rounded-lg border bg-card p-6 shadow-sm">
    <h2 class="mb-4 text-base font-semibold">{{ $t('views.SchemaEditorView.version_history') }}</h2>
    <div
      v-if="loading"
      class="space-y-2"
      role="status"
      :aria-label="$t('common.loading')"
      data-testid="schema-editor-versions-loading"
    >
      <SkeletonBlock v-for="n in 3" :key="'version-skeleton-' + n" height-class="h-10 w-full" />
    </div>
    <EmptyState v-else-if="versions.length === 0" :title="$t('views.SchemaEditorView.no_version_history')" />
    <div v-else class="space-y-2">
      <div
        v-for="version in versions"
        :key="version.id"
        class="flex items-center justify-between rounded-lg border bg-background px-3 py-2"
      >
        <div class="flex items-center gap-2">
          <span class="text-sm font-medium">v{{ version.version }}</span>
          <span
            v-if="version.published"
            class="rounded bg-success/10 px-1.5 py-0.5 text-[10px] font-medium text-success"
          >{{ $t('views.SchemaEditorView.published') }}</span>
          <span class="text-xs text-muted-foreground">{{ formatDate(version.created_at) }}</span>
        </div>
        <button type="button"
          data-testid="schema-editor-restore-version"
          class="rounded px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
          @click="$emit('restore', version)"
        >
          {{ $t('views.SchemaEditorView.restore') }}
        </button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import EmptyState from '../shared/EmptyState.vue'
import SkeletonBlock from '../shared/SkeletonBlock.vue'
import { formatDateShort } from '../../lib/formatDate'

export interface SchemaVersion {
  id: string
  schema_id: string
  version: string
  version_number: number
  definition_json: Record<string, unknown>
  published: boolean
  created_at: string
}

defineProps<{
  versions: SchemaVersion[]
  loading: boolean
}>()

defineEmits<{
  restore: [version: SchemaVersion]
}>()

function formatDate(dateStr: string): string {
  try {
    return formatDateShort(new Date(dateStr))
  } catch (e: unknown) {
    console.warn('Failed to format date', e)
    return dateStr
  }
}
</script>
