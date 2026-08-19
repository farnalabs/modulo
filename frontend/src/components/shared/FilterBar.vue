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
      <input :aria-label="search.placeholder || $t('common.search')"
        data-testid="filter-bar-search"
        :value="searchValue"
        type="text"
        :placeholder="search.placeholder || $t('common.search')"
        class="w-full rounded-lg border border-input bg-background py-2 pl-9 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring sm:w-auto"
        @input="$emit('update:search', ($event.target as HTMLInputElement).value)"
      />
    </div>
    <Select
  :aria-label="filter.label"
  v-for="filter in selectFilters"
  :key="filter.key"
  :model-value="(filterValues[filter.key] ?? '') || '__all__'"
  @update:model-value="(val) => $emit('update:filter', filter.key, val === '__all__' ? '' : String(val))"
  :placeholder="filter.label"
  :data-testid="`filter-bar-${filter.key}`"
  class="w-auto min-w-[140px]"
  :options="[{ value: '__all__', label: allLabel(filter) }, ...filter.options.map(opt => ({ value: opt.value, label: opt.label }))]"
  option-label="label"
  option-value="value"
>
  <template #header
 v-if="showLabel(filter)"
>
{{ filter.label }}
  </template>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
    <slot name="after" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Select from 'primevue/select'

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

const { t } = useI18n()

const selectFilters = computed(() => props.filters ?? [])
const filterValues = computed(() => props.filterValues ?? {})

function allLabel(filter: { key: string; label: string }): string {
  const label = filter.label.trim()
  if (!label) {
    return t('common.all')
  }
  if (label.toLowerCase() === 'all') {
    const noun = filter.key.replace(/[_-]+/g, ' ').trim()
    return noun ? `${t('common.all')} ${noun}` : t('common.all')
  }
  if (label.toLowerCase().startsWith('all ')) {
    return label
  }
  return `${t('common.all')} ${label}`
}

function showLabel(filter: { key: string; label: string }): boolean {
  const label = filter.label.trim().toLowerCase()
  return label !== 'all' && !label.startsWith('all ')
}
</script>
