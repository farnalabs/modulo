<template>
  <DataCard
    :title="title"
    :description="description"
    heading-level="h2"
    :title-class="titleClass"
    :description-class="descriptionClass"
    :data-testid="dataTestId"
    :card-class="props.class"
    variant="bordered"
    padding="md"
  >
    <template #headerActions>
      <slot name="header" />
    </template>

    <slot />
  </DataCard>
</template>

<script setup lang="ts">
import DataCard from '../ui/DataCard.vue'

const props = defineProps<{
  title: string
  description?: string
  class?: string
  titleClass?: string
  descriptionClass?: string
  dataTestId?: string
}>()
</script>
