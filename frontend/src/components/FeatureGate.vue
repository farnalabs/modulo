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
          <p class="text-sm font-medium text-warning" data-testid="feature-gate-title">
            {{ gateTitle }}
          </p>
          <p class="mt-1 text-xs text-muted-foreground">{{ tooltipText }}</p>
          <!-- No pricing link for a COMMUNITY-tier flag (FAR-1283): it is
               available on the free tier, so no plan upgrade can enable it and
               a "View Plans" call to action would send the operator somewhere
               that cannot fix the flag. -->
          <a
            v-if="!isCommunityTierGate"
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
          <h3 class="text-lg font-semibold" data-testid="feature-gate-title">{{ gateTitle }}</h3>
          <p class="text-sm text-muted-foreground">{{ tooltipText }}</p>
        </div>
        <Button
          v-if="!isCommunityTierGate"
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
import Button from 'primevue/button'
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

const enabled = computed(() => {
  if (planStore.featureEnabled(props.featureName)) return true;
  if (props.requiredTier && planStore.isAtMinimumTier(props.requiredTier)) return true;
  return false;
});

// FAR-1283: a gate on a COMMUNITY-tier flag (e.g. mcp_server) must not borrow
// the paid-tier copy. Such a flag is off because the tier is already met — the
// operator (or an org override) turned it off — so "Team Feature" and
// "Available on higher plan tier" are both false, and the "View Plans" link
// points at a purchase that cannot change the answer. Only an explicit
// community `required-tier` takes this branch; a gate with no `required-tier`
// (or an unknown tier id) keeps the pre-FAR-1283 upgrade wording.
const isCommunityTierGate = computed(() => {
  const tier = props.requiredTier;
  if (!tier) return false;
  return planStore.isCommunityTier(tier);
});

const gateTitle = computed(() =>
  isCommunityTierGate.value
    ? t("components.FeatureGate.feature_disabled")
    : t("components.FeatureGate.team_feature"),
);

const tooltipText = computed(() => {
  if (isCommunityTierGate.value) {
    return t("components.FeatureGate.disabled_for_your_organisation");
  }
  const base = props.requiredTier
    ? `${t("components.FeatureGate.available_on_higher_plan_tier")} — ${planStore.getTierLabel(props.requiredTier)}`
    : t("components.FeatureGate.available_on_higher_plan_tier");
  return `${base} — /settings/license`;
});
</script>
