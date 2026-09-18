<template>
  <div class="card p-4" :class="props.class">
    <div v-if="$slots.header" class="mb-4 flex items-center justify-between">
      <div>
        <h2 class="text-base font-semibold">{{ title }}</h2>
        <p v-if="description" class="text-sm text-muted-foreground">{{ description }}</p>
      </div>
      <slot name="header" />
    </div>
    <template v-else>
      <h2 class="mb-3 text-base font-semibold" :class="titleClass">{{ title }}</h2>
      <p v-if="description" class="mb-4 text-sm" :class="descriptionClass || 'text-muted-foreground'">{{ description }}</p>
    </template>
    <slot />
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{
  title: string
  description?: string
  class?: string
  titleClass?: string
  descriptionClass?: string
}>()
</script>
