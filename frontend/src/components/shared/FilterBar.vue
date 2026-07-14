<template>
  <div class="flex flex-wrap items-center gap-2">
    <div v-if="search" class="relative">
      <svg
        class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground pointer-events-none"
        xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      >
        <circle cx="11" cy="11" r="8" />
        <path d="m21 21-4.3-4.3" />
      </svg>
      <input aria-label="search.placeholder || "
        data-testid="filter-bar-search"
        :value="searchValue"
        type="text"
        :placeholder="search.placeholder || 'Search...'"
        class="w-full rounded-lg border border-input bg-background py-2 pl-9 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring sm:w-auto"
        @input="$emit('update:search', ($event.target as HTMLInputElement).value)"
      />
    </div>
    <select
      v-for="filter in selectFilters"
      :key="filter.key"
      :value="filterValues[filter.key] ?? ''"
      :aria-label="filter.label"
      :data-testid="`filter-bar-${filter.key}`"
      class="rounded-lg border border-input bg-background px-3 py-2 pr-10 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
      @change="$emit('update:filter', filter.key, ($event.target as HTMLSelectElement).value)"
    >
      <option value="">{{ filter.label }}</option>
      <option v-for="opt in filter.options" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
    </select>
    <slot name="after" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  search?: { placeholder?: string }
  searchValue?: string
  filters?: Array<{ key: string; label: string; options: Array<{ value: string; label: string }> }>
  filterValues?: Record<string, string>
}>()

defineEmits<{
  (e: 'update:search', value: string): void
  (e: 'update:filter', key: string, value: string): void
}>()

const selectFilters = computed(() => props.filters ?? [])
const filterValues = computed(() => props.filterValues ?? {})
</script>
