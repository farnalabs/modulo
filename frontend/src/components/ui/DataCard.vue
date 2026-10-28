<template>
  <BaseCard
    :variant="variant"
    :padding="padding"
    :elevation="elevation"
    :data-testid="dataTestId"
    :class="cardClass"
  >
    <CardHeader
      v-if="hasHeader"
      :title="title"
      :description="description"
      :title-class="titleClass"
      :description-class="descriptionClass"
      :data-testid="dataTestId ? `${dataTestId}-header` : undefined"
    >
      <template #actions>
        <slot name="headerActions" />
      </template>
    </CardHeader>

    <div
      v-if="$slots.default"
      class="card-content"
      :data-testid="dataTestId ? `${dataTestId}-content` : undefined"
    >
      <slot />
    </div>

    <div
      v-if="$slots.footer"
      class="card-footer mt-4 pt-4 border-t border-border"
      :data-testid="dataTestId ? `${dataTestId}-footer` : undefined"
    >
      <slot name="footer" />
    </div>
  </BaseCard>
</template>

<script setup lang="ts">
import BaseCard from './BaseCard.vue'
import CardHeader from './CardHeader.vue'
import { computed, useSlots } from 'vue'

const props = defineProps<{
  title?: string
  description?: string
  variant?: 'default' | 'bordered' | 'elevated' | 'muted'
  padding?: 'none' | 'sm' | 'md' | 'lg'
  elevation?: 'none' | 'sm' | 'md' | 'lg'
  titleClass?: string
  descriptionClass?: string
  dataTestId?: string
  cardClass?: string
}>()

const slots = useSlots()
const hasHeader = computed(() =>
  !!props.title || !!props.description || !!slots.headerActions
)
</script>

<style scoped>
.card-content {
  /* Content slot wrapper - can be extended with utilities if needed */
}

.card-footer {
  /* Footer wrapper with separator */
}
</style>
