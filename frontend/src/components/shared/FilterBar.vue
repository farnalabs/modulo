<template>
  <div class="flex flex-col sm:flex-row sm:flex-wrap sm:items-center gap-2">
    <div v-if="search" data-testid="filter-bar-search-wrapper" class="relative w-full sm:w-auto">
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
    <AppSelect
  :aria-label="filter.label"
  v-for="filter in selectFilters"
  :key="filter.key"
  :model-value="(filterValues[filter.key] ?? '') || ALL_VALUE"
  @update:model-value="(val: unknown) => $emit('update:filter', filter.key, val === ALL_VALUE ? '' : String(val))"
  :placeholder="filter.label"
  :data-testid="`filter-bar-${filter.key}`"
  :pt="FILTER_SELECT_PT"
  class="w-full sm:w-auto sm:min-w-[140px]"
  :options="[{ value: ALL_VALUE, label: allLabel(filter) }, ...filter.options.map(opt => ({ value: opt.value, label: opt.label }))]"
  option-label="label"
  option-value="value"
>
  <template #header v-if="showLabel(filter)">
    <!-- FAR-312: non-selectable filter-name label at the top of the dropdown.
         Padded to match the option text inset (list 0.25rem + option 0.625rem)
         so it never sits flush against the popover edge. -->
    <div
      :data-testid="`filter-bar-label-${filter.key}`"
      class="px-3.5 pt-2 pb-1 text-xs text-muted-foreground"
    >{{ filter.label }}</div>
  </template>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</AppSelect>
    <slot name="after" />
    <slot />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import AppSelect from './AppSelect.vue'

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

const ALL_VALUE = '__all__'
const { t } = useI18n()

/**
 * FAR-312 dropdown spacing/divider, applied to every filter select via
 * PrimeVue's passthrough (`pt.option`). Values are inline `style` entries on
 * purpose: the Aura theme sets `.p-select-option { padding: ...; border: 0 none }`
 * as unlayered CSS, which outranks Tailwind utility classes on the same
 * element (CSS cascade layers), while an inline style always wins.
 *
 * - every option: py 0.25rem -> 0.375rem (breathing room, ticket item 4);
 * - the `__all__` reset option (always rendered first, so `context.index === 0`)
 *   gets a bottom divider when real options follow it — visually separating
 *   the selectable reset from the real filter values (ticket item 2).
 *
 * A module-level constant keeps the object identity stable across renders so
 * passing it never forces extra child re-renders.
 */
const FILTER_SELECT_PT = {
  option: (opts: {
    context?: { index?: number }
    props?: { options?: unknown[] }
  }) => {
    const style: Record<string, string> = {
      paddingTop: '0.375rem',
      paddingBottom: '0.375rem',
    }
    const isFirstOption = opts?.context?.index === 0
    const hasRealOptions = (opts?.props?.options?.length ?? 0) > 1
    if (isFirstOption && hasRealOptions) {
      style.borderBottom = '1px solid hsl(var(--border))'
    }
    return { style }
  },
}

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
