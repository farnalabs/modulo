<template>
  <div class="relative" data-testid="feature-gate">
    <slot v-if="enabled" />
    <div
      v-else
      class="flex items-center justify-center py-16"
      data-testid="feature-gate-lock"
    >
      <div class="text-center space-y-4">
        <LockIcon :locked="true" :tooltip="tooltipText" />
        <div>
          <h3 class="text-lg font-semibold">{{ $t('components.FeatureGate.team_feature') }}</h3>
          <p class="text-sm text-muted-foreground">{{ tooltipText }}</p>
        </div>
        <a
          href="https://modulo.run/pricing"
          target="_blank"
          rel="noopener noreferrer"
          class="btn-glow inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:border-primary/60 hover:brightness-110 transition-all duration-150"
        >
          View Plans
        </a>
        <slot name="locked" :tooltip="tooltipText" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { usePlanStore } from "../stores/planStore";
import LockIcon from "./LockIcon.vue";

const props = defineProps<{
  featureName: string;
  requiredTier?: string;
}>();

const planStore = usePlanStore();

const enabled = computed(() => planStore.featureEnabled(props.featureName));

const tooltipText = computed(() => {
  if (props.requiredTier) {
    return `Available on ${planStore.getTierLabel(props.requiredTier)} plan`;
  }
  return "Available on higher plan tier";
});
</script>
