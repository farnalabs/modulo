<template>
  <div
    v-if="hasHeaderContent"
    class="card-header flex items-start justify-between gap-4 mb-4"
    :class="{ 'border-b border-border pb-3': divider }"
    :data-testid="dataTestId"
  >
    <div class="flex min-w-0 flex-1 items-start gap-3">
      <div v-if="$slots.leading" class="shrink-0 pt-0.5">
        <slot name="leading" />
      </div>
      <div class="min-w-0 flex-1">
        <component
          :is="headingLevel"
          v-if="title"
          class="text-base font-semibold"
          :class="titleClass"
        >
          {{ title }}
        </component>
        <p
          v-if="description"
          class="mt-1 text-sm"
          :class="descriptionClass || 'text-muted-foreground'"
        >
          {{ description }}
        </p>
      </div>
    </div>
    <div v-if="$slots.actions" class="flex shrink-0 items-center gap-2">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, useSlots } from 'vue'

const props = defineProps<{
  title?: string
  description?: string
  headingLevel?: 'h2' | 'h3' | 'h4'
  titleClass?: string
  descriptionClass?: string
  divider?: boolean
  dataTestId?: string
}>()

const slots = useSlots()
const headingLevel = computed(() => props.headingLevel ?? 'h3')
const hasHeaderContent = computed(() =>
  !!props.title || !!props.description || !!slots.actions || !!slots.leading
)
</script>
