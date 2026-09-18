<template>
  <section class="rounded-lg border bg-card p-6 shadow-sm">
    <h2 class="mb-4 text-base font-semibold">{{ $t('views.SchemaEditorView.version_history') }}</h2>
    <LoadingSpinner v-if="loading" />
    <p v-else-if="versions.length === 0" class="text-sm text-muted-foreground">{{ $t('views.SchemaEditorView.no_version_history') }}</p>
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
import LoadingSpinner from '../shared/LoadingSpinner.vue'
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
