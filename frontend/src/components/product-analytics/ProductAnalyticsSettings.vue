<template>
  <SectionCard
    :title="$t('views.ProductAnalytics.settings_title')"
    :description="$t('views.ProductAnalytics.settings_description')"
  >
    <div class="space-y-4">
      <ProductAnalyticsError />
      <div class="flex items-center justify-between">
        <div>
          <h4 class="text-sm font-medium">
            {{ $t('views.ProductAnalytics.opt_in_toggle_label') }}
          </h4>
          <p class="text-xs text-muted-foreground">
            {{ $t('views.ProductAnalytics.opt_in_toggle_description') }}
          </p>
        </div>
        <ToggleSwitch
          :checked="store.isOptedIn"
          :disabled="store.loading"
          :label="$t('views.ProductAnalytics.opt_in_toggle_label')"
          data-testid="product-analytics-toggle"
          @toggle="handleToggle"
        />
      </div>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div v-for="item in statItems" :key="item.label">
          <span class="text-xs font-medium text-muted-foreground">
            {{ item.label }}
          </span>
          <p class="mt-0.5" :class="{ 'text-sm text-muted-foreground': !item.badge }">
            <span v-if="item.badge" :class="item.badge">{{ item.value }}</span>
            <template v-else>{{ item.value }}</template>
          </p>
        </div>
      </div>
    </div>
  </SectionCard>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import SectionCard from '../shared/SectionCard.vue'
import ProductAnalyticsError from './ProductAnalyticsError.vue'
import ToggleSwitch from '../shared/ToggleSwitch.vue'
import { useProductAnalyticsStore } from '../../stores/productAnalyticsStore'

const { t } = useI18n()
const store = useProductAnalyticsStore()

const statItems = computed(() => [
  {
    label: t('views.ProductAnalytics.current_level'),
    value: store.isOptedIn ? t('views.ProductAnalytics.level_all') : t('views.ProductAnalytics.level_off'),
    badge: store.isOptedIn ? 'badge badge-status-success' : 'badge badge-status-muted',
  },
  {
    label: t('views.ProductAnalytics.last_successful_dump'),
    value: t('views.ProductAnalytics.coming_soon'),
    badge: null,
  },
])

onMounted(() => {
  store.fetchConsent()
})

async function handleToggle(newOptedIn: boolean) {
  await store.updateLevel(newOptedIn ? 'all' : 'off')
}
</script>
