<script setup lang="ts">
import { ref, computed } from 'vue'
import { cn } from '@/lib/utils'

export interface Column {
  key: string
  label: string
  sortable?: boolean
  numeric?: boolean
  width?: string
  testid?: string
}

export interface DataTableRow {
  [key: string]: unknown
}

const props = withDefaults(defineProps<{
  columns: Column[]
  rows: DataTableRow[]
  loading?: boolean
  loadingRows?: number
  rowClickable?: boolean
  initialSortColumn?: string | null
  initialSortDirection?: 'asc' | 'desc'
  newSortDirection?: 'asc' | 'desc'
  rowTestid?: string
}>(), {
  loading: false,
  loadingRows: 5,
  rowClickable: true,
  initialSortColumn: null,
  initialSortDirection: 'asc',
  newSortDirection: 'asc',
  rowTestid: undefined,
})

const emit = defineEmits<{
  'row-click': [row: DataTableRow]
}>()

const sortColumn = ref<string | null>(props.initialSortColumn)
const sortDirection = ref<'asc' | 'desc'>(props.initialSortDirection)

function toggleSort(key: string) {
  if (sortColumn.value === key) {
    sortDirection.value = sortDirection.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortColumn.value = key
    sortDirection.value = props.newSortDirection
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

function onRowClick(row: DataTableRow) {
  if (props.rowClickable) emit('row-click', row)
}

function onRowKeydown(event: KeyboardEvent, row: DataTableRow) {
  if (!props.rowClickable) return
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    emit('row-click', row)
  }
}
</script>

<template>
  <!-- The scroll container is focusable so a keyboard user reaches a wide
       table that has no focusable cell (a non-clickable, unsortable one) and
       can scroll it with the arrow keys; the ring makes that stop visible
       (A11Y-3). It carries no role and no handler: adding either would make it
       a static element claiming an interaction it does not have. -->
  <div
    class="overflow-x-auto overscroll-contain focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
    tabindex="0"
  >
    <table class="w-full">
      <thead>
        <tr>
          <th
            v-for="col in columns"
            :key="col.key"
            :class="cn(
              'px-4 py-3 text-xs font-medium uppercase tracking-wider text-left',
              col.numeric && 'text-right tabular-nums',
              col.sortable && 'cursor-pointer select-none hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              sortColumn === col.key ? 'text-foreground' : 'text-muted-foreground',
            )"
            :tabindex="col.sortable ? 0 : undefined"
            :aria-sort="sortColumn === col.key ? (sortDirection === 'asc' ? 'ascending' : 'descending') : undefined"
            @click="col.sortable && toggleSort(col.key)"
            @keydown.enter="col.sortable && toggleSort(col.key)"
            @keydown.space.prevent="col.sortable && toggleSort(col.key)"
          >
            <button
              v-if="col.sortable"
              type="button"
              class="inline-flex items-center gap-1"
              :data-testid="col.testid"
              tabindex="-1"
              @click.stop="toggleSort(col.key)"
              @keydown.enter.stop="toggleSort(col.key)"
              @keydown.space.prevent.stop="toggleSort(col.key)"
            >
              {{ col.label }}<span class="text-xs ml-0.5">{{ getSortIndicator(col.key) }}</span>
            </button>
            <template v-else>{{ col.label }}</template>
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
          :key="String(row.id ?? index)"
          :data-testid="rowTestid ?? (row.testid as string | undefined)"
          class="transition-colors hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
          :tabindex="props.rowClickable ? 0 : undefined"
          @click="onRowClick(row)"
          @keydown="onRowKeydown($event, row)"
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
