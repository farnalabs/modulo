<template>
  <div class="flex h-full flex-col" :data-testid="dataTestId">
    <div class="border-b p-4">
      <h2 class="font-semibold">{{ $t('views.SystemMapDetailView.revision_history') }}</h2>
      <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.SystemMapDetailView.revision_history_hint') }}</p>
    </div>
    <div class="flex gap-2 border-b p-3">
      <button v-if="!props.readOnly" type="button" class="flex-1 rounded border px-2 py-1.5 text-xs" @click="$emit('new-draft')" data-testid="system-map-new-draft">{{ $t('views.SystemMapDetailView.new_draft') }}</button>
      <button v-if="!props.readOnly" type="button" class="flex-1 rounded border px-2 py-1.5 text-xs" @click="$emit('discover')" data-testid="system-map-sidebar-discover">{{ $t('views.SystemMapDetailView.discover') }}</button>
    </div>
    <div class="space-y-2 border-b p-3">
      <button type="button" class="w-full rounded border px-2 py-1.5 text-xs" @click="$emit('select-current')" data-testid="system-map-revision-current">{{ $t('views.SystemMapDetailView.view_current') }}</button>
      <form class="flex gap-2" @submit.prevent="emitByTime">
        <input v-model="atValue" type="datetime-local" class="min-w-0 flex-1 rounded border px-2 py-1.5 text-xs" :aria-label="$t('views.SystemMapDetailView.revision_at_time')" data-testid="system-map-revision-at" />
        <button type="submit" class="rounded border px-2 py-1.5 text-xs disabled:opacity-50" :disabled="!atIso" data-testid="system-map-revision-by-time">{{ $t('views.SystemMapDetailView.view_at_time') }}</button>
      </form>
    </div>
    <div class="flex-1 overflow-y-auto p-3">
      <ol v-if="revisions.length" class="space-y-2">
        <li v-for="revision in sortedRevisions" :key="revision.id">
          <button type="button" class="w-full rounded-lg border p-3 text-left transition-colors hover:bg-accent" :class="activeRevisionId === revision.id ? 'border-primary bg-primary/5' : ''" @click="$emit('select-revision', revision.id)" :data-testid="`system-map-revision-${revision.revision_number}`">
            <div class="flex items-center justify-between gap-2">
              <span class="font-medium">{{ $t('views.SystemMapDetailView.revision', { number: revision.revision_number }) }}</span>
              <span class="flex items-center gap-1">
                <span v-if="isCurrentRevision(revision)" class="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] uppercase text-primary-text" data-testid="system-map-revision-current-badge">{{ $t('views.SystemMapDetailView.current_revision') }}</span>
                <span class="rounded px-1.5 py-0.5 text-[10px] uppercase" :class="statusClass(revision.status)">{{ revision.status }}</span>
              </span>
            </div>
            <div class="mt-1 text-xs text-muted-foreground">{{ formatDate(revision.created_at) }}</div>
             <div v-if="revision.published_at" class="mt-1 text-[10px] text-muted-foreground">{{ $t('views.SystemMapDetailView.effective_from') }}: {{ formatDate(revision.published_at) }}</div>
           </button>
           <button
             v-if="!props.readOnly && revision.status !== 'draft'"
             type="button"
             class="mt-1 w-full rounded border px-2 py-1 text-xs"
             @click="$emit('restore-revision', revision.id)"
             data-testid="system-map-revision-restore"
           >{{ $t('views.SystemMapDetailView.restore_as_new') }}</button>

        </li>
      </ol>
      <p v-else class="text-sm text-muted-foreground">{{ $t('views.SystemMapDetailView.no_revisions') }}</p>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import type { SystemMap, SystemMapRevision } from '../../stores/systemMaps'
const props = defineProps<{ mapId: string; revisions: SystemMapRevision[]; activeRevisionId?: string; activeMap: SystemMap | null; readOnly?: boolean; dataTestId?: string }>()
const emit = defineEmits<{ (e: 'select-revision', revisionId: string): void; (e: 'select-current'): void; (e: 'select-by-time', at: string): void; (e: 'new-draft'): void; (e: 'discover'): void; (e: 'restore-revision', revisionId: string): void }>()
const sortedRevisions = computed(() => [...props.revisions].filter((revision) => props.readOnly ? revision.status !== 'draft' : true).sort((a, b) => b.revision_number - a.revision_number))
const atValue = ref('')
// datetime-local carries no timezone; convert it to an absolute instant here so
// the backend's effective_from <= at < effective_to comparison is unambiguous.
const atIso = computed(() => {
  if (!atValue.value) return ''
  const date = new Date(atValue.value)
  return Number.isNaN(date.getTime()) ? '' : date.toISOString()
})
function emitByTime(): void { if (atIso.value) emit('select-by-time', atIso.value) }
function formatDate(value: string): string { return new Date(value).toLocaleString() }
function isCurrentRevision(revision: SystemMapRevision): boolean { return props.activeMap?.published_revision_id === revision.id }
function statusClass(status: string): string { return status === 'published' ? 'bg-success/10 text-success-text' : status === 'archived' ? 'bg-muted text-muted-foreground' : 'bg-warning/10 text-warning-text' }
</script>
