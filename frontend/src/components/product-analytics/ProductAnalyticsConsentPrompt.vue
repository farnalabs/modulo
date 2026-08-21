<template>
    <output
      v-if="shouldShow"
      class="block rounded-lg border bg-card p-4"
      :aria-label="$t('views.ProductAnalytics.consent_prompt_title')"
      data-testid="product-analytics-consent-prompt"
    >
      <ProductAnalyticsErrorAlert :dismiss-label="$t('views.ProductAnalytics.dismiss_error')" />
      <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div class="flex-1 space-y-1">
        <h3 class="text-sm font-semibold">
          {{ $t('views.ProductAnalytics.consent_prompt_title') }}
        </h3>
        <p class="text-sm text-muted-foreground">
          {{ promptDescription }}
        </p>
      </div>
      <div
        v-if="!store.isPartnerCarveOut"
        class="flex shrink-0 items-center gap-2"
      >
        <Button
          severity="secondary"
          outlined
          size="small"
          :disabled="store.loading"
          data-testid="product-analytics-accept"
          @click="submit('accept')"
        >
          {{ $t('views.ProductAnalytics.accept') }}
        </Button>
        <Button
          severity="secondary"
          outlined
          size="small"
          :disabled="store.loading"
          data-testid="product-analytics-decline"
          @click="submit('decline')"
        >
          {{ $t('views.ProductAnalytics.decline') }}
        </Button>
        <button
          type="button"
          class="text-xs text-muted-foreground underline underline-offset-2 hover:no-underline"
          :disabled="store.loading"
          data-testid="product-analytics-dismiss"
          @click="submit('dismiss')"
        >
          {{ $t('views.ProductAnalytics.dismiss') }}
        </button>
      </div>
      <div
        v-else
        class="flex shrink-0 items-center gap-2"
      >
        <Button
          size="small"
          :disabled="store.loading"
          data-testid="product-analytics-partner-enable"
          @click="submit('accept')"
        >
          {{ $t('views.ProductAnalytics.partner_enable') }}
        </Button>
        <button
          type="button"
          class="text-xs text-muted-foreground underline underline-offset-2 hover:no-underline"
          :disabled="store.loading"
          data-testid="product-analytics-partner-stay-community"
          @click="submit('decline')"
        >
          {{ $t('views.ProductAnalytics.partner_stay_community') }}
        </button>
      </div>
    </div>
  </output>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import { useProductAnalyticsStore } from '../../stores/productAnalyticsStore'
import ProductAnalyticsErrorAlert from './ProductAnalyticsErrorAlert.vue'

const store = useProductAnalyticsStore()
const { t } = useI18n()

const shouldShow = computed(() => store.isPromptEligible)

const promptDescription = computed(() => {
  if (store.isPartnerCarveOut) {
    return t('views.ProductAnalytics.partner_description')
  }
  return t('views.ProductAnalytics.consent_prompt_description')
})

onMounted(() => {
  if (!store.consent.prompted) {
    store.fetchConsent()
  }
})

async function submit(action: 'accept' | 'decline' | 'dismiss') {
  await store.submitConsent(action)
}
</script>
