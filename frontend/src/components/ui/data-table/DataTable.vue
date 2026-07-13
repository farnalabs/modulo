<script setup lang="ts">
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

withDefaults(defineProps<{
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
              'px-4 py-3 text-xs font-medium uppercase tracking-wider text-muted-foreground text-left',
              col.numeric && 'text-right tabular-nums',
              col.width && `w-[${col.width}]`,
            )"
          >
            {{ col.label }}
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
          v-for="(row, index) in rows"
          :key="index"
          class="transition-colors hover:bg-muted/30"
          :class="{ 'cursor-pointer': $attrs.onRowClick }"
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
