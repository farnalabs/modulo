<template>
  <BaseCard
    variant="bordered"
    padding="md"
    :data-testid="dataTestId"
    :class="cardClass"
  >
    <div class="flex flex-col gap-2">
      <p class="text-sm font-medium text-muted-foreground">
        {{ label }}
      </p>
      <p
        class="text-2xl font-semibold tabular-nums"
        :class="valueClass"
      >
        {{ value }}
      </p>
      <slot />
      <div v-if="delta != null" class="flex items-center gap-1.5">
        <span
          class="inline-flex items-center gap-0.5 text-xs font-medium"
          :class="deltaClass"
        >
          <span aria-hidden="true">{{ deltaArrow }}</span>
          {{ deltaAbsText }} ({{ deltaPctText }})
        </span>
      </div>
      <div v-else-if="progress" class="mt-1">
        <div class="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            class="h-full rounded-full transition-all duration-500"
            :class="progressBarClass"
            :style="{ width: Math.min(progress.value, progress.max) / progress.max * 100 + '%' }"
          />
        </div>
      </div>
    </div>
  </BaseCard>
</template>

<script setup lang="ts">
import BaseCard from './BaseCard.vue'
import { computed } from 'vue'

interface Progress {
  value: number
  max: number
  color?: 'primary' | 'success' | 'warning' | 'destructive'
}

const props = defineProps<{
  label: string
  value: string | number
  color?: 'primary' | 'success' | 'warning' | 'destructive' | 'muted'
  delta?: number | null
  deltaPct?: number | null
  progress?: Progress | null
  dataTestId?: string
  cardClass?: string
}>()

const valueClass = computed(() => {
  const map: Record<string, string> = {
    success: 'text-success-text',
    warning: 'text-warning-text',
    destructive: 'text-destructive',
    muted: 'text-muted-foreground',
  }
  return map[props.color ?? 'primary'] ?? ''
})

const deltaPct = computed(() => {
  const d = props.deltaPct
  return typeof d === 'number' && Number.isFinite(d) ? d : null
})

const deltaAbs = computed(() => {
  const d = props.delta
  return typeof d === 'number' && Number.isFinite(d) ? d : null
})

const deltaAbsText = computed(() => {
  const d = deltaAbs.value
  if (d == null) return ''
  if (d === 0) return '0'
  const sign = d > 0 ? '+' : '-'
  const abs = Math.abs(d)
  const formatted = Number.isInteger(abs) ? String(abs) : abs.toFixed(1)
  return `${sign}${formatted}`
})

function arrowFor(delta: number): string {
  if (delta > 0) return '▲'
  if (delta < 0) return '▼'
  return '→'
}

function classFor(delta: number): string {
  if (delta > 0) return 'text-success-text'
  if (delta < 0) return 'text-destructive'
  return 'text-muted-foreground'
}

const deltaArrow = computed(() => {
  const d = deltaPct.value ?? deltaAbs.value
  if (d == null) return ''
  return arrowFor(d)
})

const deltaClass = computed(() => {
  const d = deltaPct.value ?? deltaAbs.value
  if (d == null) return ''
  return classFor(d)
})

const deltaPctText = computed(() => {
  const d = deltaPct.value
  if (d == null) return ''
  return `${Math.abs(d).toFixed(1)}%`
})

const progressBarClass = computed(() => {
  if (!props.progress) return 'bg-primary'
  const pct = (props.progress.value / props.progress.max) * 100
  const color = props.progress.color ?? 'primary'
  if (pct >= 100) return 'bg-destructive'
  if (pct >= 90) return 'bg-warning'
  if (pct >= 75) return 'bg-amber-500'
  switch (color) {
    case 'success': return 'bg-success'
    case 'warning': return 'bg-warning'
    case 'destructive': return 'bg-destructive'
    case 'primary':
    default: return 'bg-primary'
  }
})
</script>
