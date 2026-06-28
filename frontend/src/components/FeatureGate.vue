<template>
  <div class="relative" data-testid="feature-gate">
    <div :class="{ 'pointer-events-none select-none opacity-40': !enabled }">
      <slot />
    </div>
    <div
      v-if="!enabled"
      class="absolute right-1 top-1"
      data-testid="feature-gate-lock"
    >
      <LockIcon :locked="true" :tooltip="tooltipText" />
    </div>
    <slot v-if="!enabled" name="locked" :tooltip="tooltipText" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { usePlanStore } from '../stores/planStore'
import LockIcon from './LockIcon.vue'

const props = defineProps<{
  featureName: string
  requiredTier?: string
}>()

const planStore = usePlanStore()

const enabled = computed(() => planStore.featureEnabled(props.featureName))

const tooltipText = computed(() => {
  if (props.requiredTier) {
    return `Available on ${props.requiredTier} plan`
  }
  return 'Available on higher plan tier'
})
</script>
