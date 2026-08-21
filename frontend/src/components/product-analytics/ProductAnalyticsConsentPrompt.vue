<template>
  <div
    v-if="shouldShow"
    class="rounded-lg border bg-card p-4"
    role="status"
    :aria-label="$t('views.ProductAnalytics.consent_prompt_title')"
    data-testid="product-analytics-consent-prompt"
  >
    <div
      v-if="store.error"
      class="rounded-md bg-destructive/10 p-3 text-sm text-destructive"
      role="alert"
    >
      {{ store.error }}
      <button class="ml-2 underline" @click="store.error = null">{{ $t('views.ProductAnalytics.dismiss_error') }}</button>
    </div>
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
          @click="handleAccept"
        >
          {{ $t('views.ProductAnalytics.accept') }}
        </Button>
        <Button
          severity="secondary"
          outlined
          size="small"
          :disabled="store.loading"
          data-testid="product-analytics-decline"
          @click="handleDecline"
        >
          {{ $t('views.ProductAnalytics.decline') }}
        </Button>
        <button
          class="text-xs text-muted-foreground underline underline-offset-2 hover:no-underline"
          :disabled="store.loading"
          data-testid="product-analytics-dismiss"
          @click="handleDismiss"
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
          @click="handleAccept"
        >
          {{ $t('views.ProductAnalytics.partner_enable') }}
        </Button>
        <button
          class="text-xs text-muted-foreground underline underline-offset-2 hover:no-underline"
          :disabled="store.loading"
          data-testid="product-analytics-partner-stay-community"
          @click="handleDecline"
        >
          {{ $t('views.ProductAnalytics.partner_stay_community') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import { useProductAnalyticsStore } from '../../stores/productAnalyticsStore'

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

async function handleAccept() {
  await store.submitConsent('accept')
}

async function handleDecline() {
  await store.submitConsent('decline')
}

async function handleDismiss() {
  await store.submitConsent('dismiss')
}
</script>
