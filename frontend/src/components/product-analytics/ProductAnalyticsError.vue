<template>
  <ErrorAlert
    v-if="store.error"
    :message="store.error ?? undefined"
    :on-dismiss="dismissError"
    :dismiss-label="$t('views.ProductAnalytics.dismiss_error')"
  />
</template>

<script setup lang="ts">
import ErrorAlert from '../shared/ErrorAlert.vue'
import { useProductAnalyticsStore } from '../../stores/productAnalyticsStore'

const store = useProductAnalyticsStore()

function dismissError() {
  store.error = null
}
</script>
