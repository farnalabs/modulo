<template>
  <BaseCard variant="bordered" padding="lg" :data-testid="dataTestId" :class="cardClass">
    <CardHeader
      v-if="hasHeader"
      :title="title"
      :description="description"
      :data-testid="dataTestId ? `${dataTestId}-header` : undefined"
    >
      <template #actions><slot name="headerActions" /></template>
    </CardHeader>

    <div
      class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5"
      data-testid="risk-assessment-summary-grid"
    >
      <div
        v-for="(item, index) in items"
        :key="item.testId ?? index"
        class="rounded-lg border bg-card p-4"
        :data-testid="item.testId"
      >
        <div class="text-3xl font-bold" :class="colorClass(item)">{{ item.value }}</div>
        <div class="text-sm text-muted-foreground">{{ item.label }}</div>
      </div>
    </div>

    <p v-if="footnote" class="mt-4 text-sm text-muted-foreground" :data-testid="footnoteTestId">
      {{ footnote }}
    </p>
    <slot name="footer" />
  </BaseCard>
</template>

<script setup lang="ts">
import { computed, useSlots } from 'vue'
import BaseCard from './BaseCard.vue'
import CardHeader from './CardHeader.vue'

interface SummaryItem {
  label: string
  value: string | number
  color?: 'primary' | 'success' | 'warning' | 'destructive' | 'muted' | 'foreground'
  testId?: string
}

const props = withDefaults(defineProps<{
  title?: string
  description?: string
  items: SummaryItem[]
  facetDegraded?: boolean
  footnote?: string
  dataTestId?: string
  gridTestId?: string
  footnoteTestId?: string
  cardClass?: string
}>(), {
  gridTestId: undefined,
  footnoteTestId: undefined,
})

const slots = useSlots()
const hasHeader = computed(() => !!props.title || !!props.description || !!slots.headerActions)

const colorClass = (item: SummaryItem): string => {
  const map: Record<string, string> = {
    primary: 'text-primary-text',
    success: 'text-success-text',
    warning: 'text-warning-text',
    destructive: 'text-destructive-text',
    muted: 'text-muted-foreground',
    foreground: 'text-foreground',
  }
  return map[item.color ?? 'foreground'] ?? 'text-foreground'
}
</script>
