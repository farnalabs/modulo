<template>
    <div
      v-if="shouldShow"
      role="region"
      class="block rounded-lg border bg-card p-4"
      :aria-label="$t('views.ProductAnalytics.consent_prompt_title')"
      data-testid="product-analytics-consent-prompt"
    >
      <ProductAnalyticsError />
      <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div class="flex-1 space-y-1">
        <h3 class="text-sm font-semibold">
          {{ $t('views.ProductAnalytics.consent_prompt_title') }}
        </h3>
        <p class="text-sm text-muted-foreground">
          {{ promptDescription }}
        </p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <Button
          v-for="action in promptActions"
          :key="action.key"
          :severity="action.variant === 'button-secondary' ? 'secondary' : undefined"
          :outlined="action.variant === 'button-secondary'"
          :text="action.variant === 'text'"
          size="small"
          :class="action.variant === 'text' ? 'text-xs text-muted-foreground underline underline-offset-2 hover:no-underline' : ''"
          :disabled="store.loading"
          :data-testid="action.testid"
          @click="submit(action.action)"
        >
          {{ t(action.labelKey) }}
        </Button>
      </div>
      </div>
    </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import ProductAnalyticsError from './ProductAnalyticsError.vue'
import { useProductAnalyticsStore } from '../../stores/productAnalyticsStore'

const store = useProductAnalyticsStore()
const { t } = useI18n()

const shouldShow = computed(() => store.isPromptEligible)

const promptDescription = computed(() => t('views.ProductAnalytics.consent_prompt_description'))

type PromptAction = {
  key: string
  labelKey: string
  testid: string
  variant: 'button-secondary' | 'button' | 'text'
  action: 'accept' | 'decline' | 'dismiss'
}

type PromptActionDef = [
  key: string,
  labelKey: string,
  testid: string,
  variant: PromptAction['variant'],
  action: PromptAction['action'],
]

const standardActions: PromptActionDef[] = [
  ['accept', 'views.ProductAnalytics.accept', 'product-analytics-accept', 'button-secondary', 'accept'],
  ['decline', 'views.ProductAnalytics.decline', 'product-analytics-decline', 'button-secondary', 'decline'],
  ['dismiss', 'views.ProductAnalytics.dismiss', 'product-analytics-dismiss', 'text', 'dismiss'],
]

const buildPromptActions = (defs: PromptActionDef[]): PromptAction[] =>
  defs.map(([key, labelKey, testid, variant, action]) => ({ key, labelKey, testid, variant, action }))

const promptActions = computed<PromptAction[]>(() => buildPromptActions(standardActions))

onMounted(() => {
  if (!store.consent.prompted) {
    store.fetchConsent()
  }
})

async function submit(action: 'accept' | 'decline' | 'dismiss') {
  await store.submitConsent(action)
}
</script>
