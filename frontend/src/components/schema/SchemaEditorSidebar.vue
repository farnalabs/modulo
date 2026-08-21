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
      <LoadingSpinner v-if="loading" />
      <div v-else-if="schemas.length === 0" class="p-4 text-center text-sm text-muted-foreground">
        {{ $t('views.SchemaEditorView.no_schemas_yet') }}
      </div>
      <template v-else>
        <div
          role="button"
          tabindex="0"
          @keydown.enter="($event.currentTarget as HTMLElement).click()"
          @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
          v-for="schema in schemas"
          :key="schema.id"
          class="cursor-pointer border-b px-4 py-3 transition-colors hover:bg-muted/50"
          :class="{ 'bg-muted': selectedId === schema.id }"
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
        </div>
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
import LoadingSpinner from '../shared/LoadingSpinner.vue'
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
