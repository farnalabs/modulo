<template>
  <aside class="flex w-80 flex-col border-r bg-background">
    <div class="border-b p-4">
      <h2 class="text-base font-semibold">{{ $t('views.SchemaEditorView.schemas') }}</h2>
      <div class="mt-2">
        <FilterBar
          :search="{ placeholder: $t('views.SchemaEditorView.search_schemas') }"
          :search-value="searchQuery"
          @update:search="$emit('update:searchQuery', $event)"
        />
      </div>
    </div>

    <div class="flex-1 overflow-y-auto">
      <div
        v-if="loading"
        class="space-y-2 p-4"
        role="status"
        :aria-label="$t('common.loading')"
        data-testid="schema-editor-list-loading"
      >
        <SkeletonBlock v-for="n in 5" :key="'schema-skeleton-' + n" height-class="h-12 w-full" />
      </div>
      <div v-else-if="schemas.length === 0" class="p-4">
        <EmptyState :title="$t('views.SchemaEditorView.no_schemas_yet')" />
      </div>
      <template v-else>
        <button
          type="button"
          v-for="schema in schemas"
          :key="schema.id"
          class="w-full border-b px-4 py-3 text-left transition-colors hover:bg-muted/50"
          :class="{ 'bg-muted': selectedId === schema.id }"
          :aria-current="selectedId === schema.id ? 'true' : undefined"
          data-testid="schema-editor-list-item"
          @click="$emit('select', schema.id)"
        >
          <div class="flex items-center justify-between">
            <span class="text-sm font-medium">{{ schema.name }}</span>
            <span
              v-if="schema.deprecated"
              class="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] font-medium text-destructive"
            >{{ $t('views.SchemaEditorView.deprecated') }}</span>
          </div>
          <p v-if="schema.description" class="mt-0.5 truncate text-xs text-muted-foreground">{{ schema.description }}</p>
        </button>
      </template>
    </div>

    <div class="border-t p-4">
      <Button class="w-full" data-testid="schema-editor-new" @click="$emit('create')">
        {{ $t('views.SchemaEditorView.new_schema') }}
      </Button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import FilterBar from '../shared/FilterBar.vue'
import EmptyState from '../shared/EmptyState.vue'
import SkeletonBlock from '../shared/SkeletonBlock.vue'
import Button from 'primevue/button'
import type { components } from '../../lib/api/client'

export type SchemaItem = components['schemas']['modulo__api__routes__schemas__SchemaResponse']

defineProps<{
  schemas: SchemaItem[]
  loading: boolean
  selectedId: string | null
  searchQuery: string
}>()

defineEmits<{
  select: [id: string]
  create: []
  'update:searchQuery': [value: string]
}>()
</script>
