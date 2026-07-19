<script setup lang="ts">
import { ref, computed } from 'vue'
import { cn } from '@/lib/utils'

export interface Column {
  key: string
  label: string
  sortable?: boolean
  numeric?: boolean
  width?: string
}

export interface DataTableRow {
  [key: string]: unknown
}

const props = withDefaults(defineProps<{
  columns: Column[]
  rows: DataTableRow[]
  loading?: boolean
  loadingRows?: number
}>(), {
  loading: false,
  loadingRows: 5,
})

const emit = defineEmits<{
  'row-click': [row: DataTableRow]
}>()

const sortColumn = ref<string | null>(null)
const sortDirection = ref<'asc' | 'desc'>('asc')

function toggleSort(key: string) {
  if (sortColumn.value === key) {
    sortDirection.value = sortDirection.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortColumn.value = key
    sortDirection.value = 'asc'
  }
}

function getSortIndicator(key: string): string {
  if (sortColumn.value !== key) return ''
  return sortDirection.value === 'asc' ? ' ▲' : ' ▼'
}

const sortedRows = computed(() => {
  if (!sortColumn.value) return props.rows
  const col = props.columns.find(c => c.key === sortColumn.value)
  if (!col?.sortable) return props.rows

  const key = sortColumn.value
  const dir = sortDirection.value === 'asc' ? 1 : -1

  return [...props.rows].sort((a, b) => {
    const aVal = a[key]
    const bVal = b[key]
    if (aVal == null && bVal == null) return 0
    if (aVal == null) return 1
    if (bVal == null) return -1

    if (typeof aVal === 'number' && typeof bVal === 'number') {
      return (aVal - bVal) * dir
    }
    const aStr = String(aVal)
    const bStr = String(bVal)
    return aStr.localeCompare(bStr) * dir
  })
})
</script>

<template>
  <div class="overflow-x-auto">
    <table class="w-full">
      <thead>
        <tr>
          <th
            v-for="col in columns"
            :key="col.key"
            :class="cn(
              'px-4 py-3 text-xs font-medium uppercase tracking-wider text-left',
              col.numeric && 'text-right tabular-nums',
              col.sortable && 'cursor-pointer select-none hover:text-foreground',
              sortColumn === col.key ? 'text-foreground' : 'text-muted-foreground',
            )"
            @click="col.sortable && toggleSort(col.key)"
          >
            {{ col.label }}<span v-if="col.sortable" class="text-xs ml-0.5">{{ getSortIndicator(col.key) }}</span>
          </th>
        </tr>
      </thead>
      <tbody v-if="loading" class="divide-y divide-border">
        <tr v-for="i in loadingRows" :key="i">
          <td v-for="col in columns" :key="col.key" :class="cn('px-4 py-3', col.numeric && 'text-right')">
            <div class="h-4 animate-pulse rounded bg-muted" :style="{ width: `${30 + (i * 7) % 50}%` }" />
          </td>
        </tr>
      </tbody>
      <tbody v-else-if="rows.length === 0" class="divide-y divide-border">
        <tr>
          <td :colspan="columns.length" class="px-4 py-8 text-center text-sm text-muted-foreground">
            <slot name="empty">
              No data available.
            </slot>
          </td>
        </tr>
      </tbody>
      <tbody v-else class="divide-y divide-border">
        <tr
          v-for="(row, index) in sortedRows"
          :key="index"
          class="transition-colors hover:bg-muted/30 cursor-pointer"
          role="button"
          tabindex="0"
          @click="emit('row-click', row)"
          @keydown.enter="emit('row-click', row)"
          @keydown.space.prevent="emit('row-click', row)"
        >
          <td
            v-for="col in columns"
            :key="col.key"
            :class="cn(
              'px-4 py-3 text-sm',
              col.numeric && 'text-right tabular-nums',
            )"
          >
            <slot :name="`cell-${col.key}`" :row="row" :value="row[col.key]">
              {{ row[col.key] }}
            </slot>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
