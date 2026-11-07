<template>
  <header class="border-b bg-card px-4 py-3 flex flex-wrap items-center gap-3" :data-testid="dataTestId">
    <div class="flex-1 min-w-0">
      <h1 class="text-lg font-semibold truncate">{{ map?.name ?? $t('views.SystemMapDetailView.title') }}</h1>
      <p class="text-xs text-muted-foreground mt-0.5">
        <span class="capitalize">{{ revision?.status ?? $t('views.SystemMapDetailView.draft') }}</span>
        <span v-if="revision"> · {{ $t('views.SystemMapDetailView.revision', { number: revision.revision_number }) }}</span>
        <span v-if="readOnly" class="ml-2 rounded bg-muted px-1.5 py-0.5">{{ $t('views.SystemMapDetailView.read_only') }}</span>
      </p>
    </div>
    <div class="flex items-center gap-2">
      <button type="button" class="rounded border px-2.5 py-1.5 text-sm" :aria-label="$t('views.SystemMapDetailView.toggle_history')" @click="$emit('toggle-sidebar')" data-testid="system-map-header-toggle-history">
        {{ $t('views.SystemMapDetailView.history') }}
      </button>
      <button type="button" class="rounded border px-2.5 py-1.5 text-sm" :disabled="readOnly || saving" @click="$emit('save')" data-testid="system-map-save-draft">
        {{ saving ? $t('views.SystemMapDetailView.saving') : $t('views.SystemMapDetailView.save_draft') }}
      </button>
       <button v-if="!readOnly" type="button" class="rounded border px-2.5 py-1.5 text-sm" @click="$emit('discover')" data-testid="system-map-discover">
         {{ $t('views.SystemMapDetailView.discover') }}
       </button>
      <button type="button" class="rounded bg-primary px-2.5 py-1.5 text-sm text-primary-foreground" :disabled="props.readOnly || !revision || revision.status !== 'draft'" @click="$emit('publish')" data-testid="system-map-publish">
        {{ $t('views.SystemMapDetailView.publish') }}
      </button>
      <button v-if="revision?.status === 'published'" type="button" class="rounded border px-2.5 py-1.5 text-sm" :disabled="props.readOnly" @click="$emit('archive')" data-testid="system-map-archive">
        {{ $t('views.SystemMapDetailView.archive') }}
      </button>
    </div>
  </header>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { SystemMap, SystemMapRevision } from '../../stores/systemMaps'
const props = defineProps<{ map: SystemMap | null; revision: SystemMapRevision | null; saving?: boolean; readOnly?: boolean; dataTestId?: string }>()
defineEmits<{ (e: 'toggle-sidebar'): void; (e: 'save'): void; (e: 'publish'): void; (e: 'archive'): void; (e: 'discover'): void }>()
const readOnly = computed(() => props.readOnly === true || props.revision?.status === 'published' || props.revision?.status === 'archived')
</script>
