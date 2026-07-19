<template>
  <div class="relative" data-testid="feature-gate">
    <slot v-if="enabled" />

    <div
      v-else-if="showDisabled"
      class="relative min-h-[300px]"
      data-testid="feature-gate-disabled"
    >
      <div class="pointer-events-none select-none opacity-40">
        <slot />
      </div>
      <div class="absolute inset-0 flex items-start justify-center pt-8">
        <div
          class="mx-4 rounded-lg border border-warning/30 bg-background/95 p-4 text-center shadow-lg backdrop-blur-sm"
        >
          <p class="text-sm font-medium text-warning">
            {{ $t('components.FeatureGate.team_feature') }}
          </p>
          <p class="mt-1 text-xs text-muted-foreground">{{ tooltipText }}</p>
          <a
            :href="pricingUrl"
            target="_blank"
            rel="noopener noreferrer"
            class="mt-2 inline-block text-xs font-semibold text-primary hover:underline"
          >
            {{ $t('components.FeatureGate.view_plans') }}
          </a>
          <slot name="locked" :tooltip="tooltipText" />
        </div>
      </div>
    </div>

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
        <Button
          variant="default"
          as="a"
          :href="pricingUrl"
          target="_blank"
          rel="noopener noreferrer"
           class="border-primary/30 hover:border-primary/60"
        >
          {{ $t('components.FeatureGate.view_plans') }}
        </Button>
        <slot name="locked" :tooltip="tooltipText" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { Button } from "@/components/ui/button";
import { useI18n } from "vue-i18n";
import { usePlanStore } from "../stores/planStore";
import LockIcon from "./LockIcon.vue";

const { t } = useI18n();

const props = withDefaults(defineProps<{
  featureName: string;
  requiredTier?: string;
  showDisabled?: boolean;
  pricingUrl?: string;
}>(), {
  pricingUrl: "/settings/license",
});

const planStore = usePlanStore();

const enabled = computed(() => planStore.featureEnabled(props.featureName));

const tooltipText = computed(() => {
  if (props.requiredTier) {
    return `${t("components.FeatureGate.available_on_higher_plan_tier")} — ${planStore.getTierLabel(props.requiredTier)}`;
  }
  return t("components.FeatureGate.available_on_higher_plan_tier");
});
</script>
