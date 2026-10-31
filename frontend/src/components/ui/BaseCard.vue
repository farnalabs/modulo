<template>
  <div
    :class="[
      'card',
      'card-base',
      variantClass,
      paddingClass,
      elevationClass,
      props.class,
    ]"
    :data-testid="dataTestId"
  >
    <slot name="header" />
    <slot />
    <slot name="footer" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  variant?: 'default' | 'bordered' | 'elevated' | 'muted' | 'context'
  padding?: 'none' | 'sm' | 'md' | 'lg'
  elevation?: 'none' | 'sm' | 'md' | 'lg'
  class?: string
  dataTestId?: string
}>()

const variantClass = computed(() => {
  switch (props.variant) {
    case 'bordered':
      return 'bg-card border border-border'
    case 'elevated':
      return 'bg-card shadow'
    case 'muted':
      return 'bg-muted/50'
    case 'context':
      return 'bg-context-purple/10 border border-context-purple/20'
    case 'default':
    default:
      return 'bg-card'
  }
})

const paddingClass = computed(() => {
  switch (props.padding) {
    case 'none':
      return 'p-0'
    case 'sm':
      return 'p-3'
    case 'md':
      return 'p-4'
    case 'lg':
      return 'p-6'
    default:
      return 'p-4'
  }
})

const elevationClass = computed(() => {
  switch (props.elevation) {
    case 'none':
      return ''
    case 'sm':
      return 'shadow-sm'
    case 'md':
      return 'shadow'
    case 'lg':
      return 'shadow-lg'
    default:
      return ''
  }
})
</script>

<style scoped>
.card-base {
  border-radius: var(--radius-lg);
  color: hsl(var(--card-foreground));
}
</style>
