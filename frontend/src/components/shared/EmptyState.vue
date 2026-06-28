<template>
  <div class="card p-8 text-center">
    <component :is="iconComponent" v-if="icon" class="mx-auto mb-3 h-12 w-12 text-muted-foreground" />
    <p class="text-lg font-medium">{{ title }}</p>
    <p v-if="description" class="mt-1 text-sm text-muted-foreground">{{ description }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  title: string
  description?: string
  icon?: string
}>()

const iconComponent = computed(() => {
  if (props.icon === 'message-square') {
    return {
      template: '<svg class="h-12 w-12 text-muted-foreground" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>'
    }
  }
  return undefined
})
</script>
