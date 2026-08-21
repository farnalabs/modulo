<template>
  <SectionCard
    :title="$t('views.ProductAnalytics.settings_title')"
    :description="$t('views.ProductAnalytics.settings_description')"
  >
    <div class="space-y-4">
      <ProductAnalyticsErrorAlert :dismiss-label="$t('views.ProductAnalytics.dismiss_error')" />
      <div class="flex items-center justify-between">
        <div>
          <h4 class="text-sm font-medium">
            {{ $t('views.ProductAnalytics.opt_in_toggle_label') }}
          </h4>
          <p class="text-xs text-muted-foreground">
            {{ $t('views.ProductAnalytics.opt_in_toggle_description') }}
          </p>
        </div>
        <span
          class="inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full transition-colors"
          role="switch"
          :aria-checked="store.isOptedIn"
          :aria-label="$t('views.ProductAnalytics.opt_in_toggle_label')"
          tabindex="0"
          :disabled="store.loading"
          data-testid="product-analytics-toggle"
          @click="handleToggle"
          @keydown.enter="handleToggle"
          @keydown.space.prevent="handleToggle"
          :class="store.isOptedIn ? 'bg-primary' : 'bg-input'"
        >
          <span
            class="inline-block h-4 w-4 rounded-full bg-background shadow-sm transition-transform"
            :class="store.isOptedIn ? 'translate-x-[18px]' : 'translate-x-0.5'"
          />
        </span>
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
import SectionCard from '../shared/SectionCard.vue'
import { useProductAnalyticsStore } from '../../stores/productAnalyticsStore'
import ProductAnalyticsErrorAlert from './ProductAnalyticsErrorAlert.vue'

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

async function handleToggle() {
  const newLevel = store.isOptedIn ? 'off' : 'all'
  await store.updateLevel(newLevel)
}
</script>
