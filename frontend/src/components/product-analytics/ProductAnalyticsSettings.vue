<template>
  <SectionCard
    :title="$t('views.ProductAnalytics.settings_title')"
    :description="$t('views.ProductAnalytics.settings_description')"
  >
    <div class="space-y-4">
      <div
        v-if="store.error"
        class="rounded-md bg-destructive/10 p-3 text-sm text-destructive"
        role="alert"
      >
        {{ store.error }}
        <button class="ml-2 underline" @click="store.error = null">Dismiss</button>
      </div>
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
        <div>
          <span class="text-xs font-medium text-muted-foreground">
            {{ $t('views.ProductAnalytics.current_level') }}
          </span>
          <p class="mt-0.5">
            <span :class="store.isOptedIn ? 'badge badge-status-success' : 'badge badge-status-muted'">
              {{ store.isOptedIn ? $t('views.ProductAnalytics.level_all') : $t('views.ProductAnalytics.level_off') }}
            </span>
          </p>
        </div>
        <div>
          <span class="text-xs font-medium text-muted-foreground">
            {{ $t('views.ProductAnalytics.last_successful_dump') }}
          </span>
          <p class="mt-0.5 text-sm text-muted-foreground">
            {{ $t('views.ProductAnalytics.coming_soon') }}
          </p>
        </div>
      </div>
    </div>
  </SectionCard>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import SectionCard from '../shared/SectionCard.vue'
import { useProductAnalyticsStore } from '../../stores/productAnalyticsStore'

const store = useProductAnalyticsStore()

onMounted(() => {
  store.fetchConsent()
})

async function handleToggle() {
  const newLevel = store.isOptedIn ? 'off' : 'all'
  await store.updateLevel(newLevel)
}
</script>
