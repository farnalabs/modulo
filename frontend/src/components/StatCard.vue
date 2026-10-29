<template>
  <component :is="to ? 'router-link' : 'div'" :to="to" class="block">
    <BaseCard
      variant="bordered"
      padding="md"
       :data-testid="dataTestId ?? 'dashboard-stats-card'"
      :class="`card-hover ${props.class ?? ''}`"
    >
      <div class="flex items-center gap-3">
        <div
          class="flex h-9 w-9 items-center justify-center rounded-lg"
          :class="iconBgClass"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            aria-hidden="true"
          >
            <slot name="icon" />
          </svg>
        </div>
        <div class="min-w-0 flex-1">
          <p class="text-sm font-medium text-muted-foreground">{{ label }}</p>
          <div class="flex items-baseline gap-2">
            <p class="text-2xl font-semibold tabular-nums" :class="valueClass">{{ value }}</p>
            <span
              v-if="deltaPct != null"
              class="inline-flex items-center gap-0.5 text-xs font-medium"
              :class="deltaClass"
            >
              <span aria-hidden="true">{{ deltaArrow }}</span>{{ deltaAbsText }} ({{ deltaPctText }})
            </span>
            <span
              v-else-if="deltaAbs != null && noBaselineLabel"
              class="inline-flex items-center gap-0.5 text-xs font-medium"
              :class="deltaAbsClass"
              data-testid="stat-no-baseline"
              :title="noBaselineLabel"
            >
              <span aria-hidden="true">{{ deltaAbsArrow }}</span>{{ deltaAbsText }}
              <span class="sr-only">{{ noBaselineLabel }}</span>
            </span>
          </div>
        </div>
      </div>
    </BaseCard>
  </component>
</template>

<script setup lang="ts">
import BaseCard from './ui/BaseCard.vue'
import { computed } from 'vue'
import {
  formatDeltaAbs,
  useDeltaPctText,
  useDeltaArrow,
  useDeltaClass,
} from '../composables/useDeltaFormatting'

export interface StatDelta {
  current?: number | null
  previous?: number | null
  delta_pct?: number | null
}

const props = defineProps<{
  label: string
  value: number | string
  color?: 'primary' | 'success' | 'warning' | 'destructive' | 'muted'
  to?: string
  delta?: StatDelta | null
  noBaselineLabel?: string
  inverted?: boolean
  neutralTrend?: boolean
  class?: string
  dataTestId?: string
}>()

const iconBgClass = computed(() => {
  const map: Record<string, string> = {
    primary: 'bg-primary/10 text-primary-text',
    success: 'bg-success/10 text-success-text',
    warning: 'bg-warning/10 text-warning-text',
    destructive: 'bg-destructive/10 text-destructive-text',
    muted: 'bg-muted text-muted-foreground',
  }
  return map[props.color ?? 'primary']
})

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
  const d = props.delta?.delta_pct
  return typeof d === 'number' && Number.isFinite(d) ? d : null
})

const deltaAbs = computed(() => {
  const { current, previous } = props.delta ?? {}
  if (typeof current !== 'number' || typeof previous !== 'number') return null
  if (!Number.isFinite(current) || !Number.isFinite(previous)) return null
  return current - previous
})

const deltaAbsText = computed(() => formatDeltaAbs(deltaAbs.value))
const deltaPctText = useDeltaPctText(deltaPct)
const deltaArrow = useDeltaArrow(deltaPct)
const deltaAbsArrow = useDeltaArrow(deltaAbs)
const deltaClass = useDeltaClass(deltaPct, computed(() => props.inverted ?? false), computed(() => props.neutralTrend ?? false))
const deltaAbsClass = useDeltaClass(deltaAbs, computed(() => props.inverted ?? false), computed(() => props.neutralTrend ?? false))
</script>

<style scoped>
.stagger-item:nth-child(1) { animation-delay: 0ms; }
.stagger-item:nth-child(2) { animation-delay: 40ms; }
.stagger-item:nth-child(3) { animation-delay: 80ms; }
.stagger-item:nth-child(4) { animation-delay: 120ms; }
.stagger-item:nth-child(5) { animation-delay: 160ms; }
.stagger-item:nth-child(6) { animation-delay: 200ms; }
</style>
