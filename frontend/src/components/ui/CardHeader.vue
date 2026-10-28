<template>
  <div
    v-if="hasHeaderContent"
    class="card-header flex items-start justify-between gap-4 mb-4"
    :data-testid="dataTestId"
  >
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
        class="mt-1 text-sm text-muted-foreground"
        :class="descriptionClass"
      >
        {{ description }}
      </p>
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
  dataTestId?: string
}>()

const slots = useSlots()
const headingLevel = computed(() => props.headingLevel ?? 'h3')
const hasHeaderContent = computed(() =>
  !!props.title || !!props.description || !!slots.actions
)
</script>
