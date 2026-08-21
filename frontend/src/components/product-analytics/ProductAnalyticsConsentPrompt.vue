<template>
    <output
      v-if="shouldShow"
      class="block rounded-lg border bg-card p-4"
      :aria-label="$t('views.ProductAnalytics.consent_prompt_title')"
      data-testid="product-analytics-consent-prompt"
    >
      <ErrorAlert
        v-if="store.error"
        :message="errorMessage"
        :on-dismiss="dismissError"
        :dismiss-label="$t('views.ProductAnalytics.dismiss_error')"
      />
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
  </output>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import ErrorAlert from '../shared/ErrorAlert.vue'
import { useProductAnalyticsStore } from '../../stores/productAnalyticsStore'

const store = useProductAnalyticsStore()
const { t } = useI18n()

const errorMessage = computed(() => store.error ?? undefined)
function dismissError() {
  store.error = null
}

const shouldShow = computed(() => store.isPromptEligible)

const promptDescription = computed(() => {
  if (store.isPartnerCarveOut) {
    return t('views.ProductAnalytics.partner_description')
  }
  return t('views.ProductAnalytics.consent_prompt_description')
})

type PromptAction = {
  key: string
  labelKey: string
  testid: string
  variant: 'button-secondary' | 'button' | 'text'
  action: 'accept' | 'decline' | 'dismiss'
}

const promptActions = computed<PromptAction[]>(() =>
  store.isPartnerCarveOut
    ? [
        {
          key: 'partner-enable',
          labelKey: 'views.ProductAnalytics.partner_enable',
          testid: 'product-analytics-partner-enable',
          variant: 'button',
          action: 'accept',
        },
        {
          key: 'partner-stay-community',
          labelKey: 'views.ProductAnalytics.partner_stay_community',
          testid: 'product-analytics-partner-stay-community',
          variant: 'text',
          action: 'decline',
        },
      ]
    : [
        {
          key: 'accept',
          labelKey: 'views.ProductAnalytics.accept',
          testid: 'product-analytics-accept',
          variant: 'button-secondary',
          action: 'accept',
        },
        {
          key: 'decline',
          labelKey: 'views.ProductAnalytics.decline',
          testid: 'product-analytics-decline',
          variant: 'button-secondary',
          action: 'decline',
        },
        {
          key: 'dismiss',
          labelKey: 'views.ProductAnalytics.dismiss',
          testid: 'product-analytics-dismiss',
          variant: 'text',
          action: 'dismiss',
        },
      ],
)

onMounted(() => {
  if (!store.consent.prompted) {
    store.fetchConsent()
  }
})

async function submit(action: 'accept' | 'decline' | 'dismiss') {
  await store.submitConsent(action)
}
</script>
