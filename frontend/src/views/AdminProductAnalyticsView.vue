<template>
  <FeatureGate feature-name="product_analytics" required-tier="team" show-disabled>

    <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.AdminProductAnalyticsView.title')" :subtitle="$t('views.AdminProductAnalyticsView.subtitle')" />

    <LoadingSpinner v-if="store.isLoading" />
    <ErrorAlert v-else-if="store.error" :message="store.error" :on-retry="store.fetchTransparency" />

    <template v-else-if="store.transparency">
      <div class="space-y-6">
        <!-- Warning banner -->
        <div v-if="store.transparency.warning === 'not_reaching_farnalabs'" class="rounded-lg border border-amber-300 bg-amber-50 p-4 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200">
          <p class="text-sm font-medium">{{ $t('views.AdminProductAnalyticsView.warning_not_reaching') }}</p>
        </div>

        <!-- Transparency data -->
        <SectionCard :title="$t('views.AdminProductAnalyticsView.delivery_status')">
          <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.last_successful_dump') }}</span>
              <p class="mt-0.5 text-lg font-semibold" data-testid="last-dump">
                {{ store.transparency.last_successful_dump_at ? formatDate(store.transparency.last_successful_dump_at) : '—' }}
              </p>
            </div>
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.total_dumps') }}</span>
              <p class="mt-0.5 text-lg font-semibold" data-testid="dump-count-total">
                {{ store.transparency.dump_count_total }}
              </p>
            </div>
          </div>
        </SectionCard>

        <SectionCard :title="$t('views.AdminProductAnalyticsView.consent_and_enforcement')">
          <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.consent_level') }}</span>
              <p class="mt-0.5">
                <span :class="store.transparency.consent_level === 'all' ? 'badge badge-status-success' : 'badge badge-status-muted'" data-testid="consent-level">
                  {{ consentLevelLabel }}
                </span>
              </p>
            </div>
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.instance_switch') }}</span>
              <p class="mt-0.5">
                <span :class="store.transparency.instance_enabled ? 'badge badge-status-success' : 'badge badge-status-muted'" data-testid="instance-enabled">
                  {{ store.transparency.instance_enabled ? $t('views.AdminProductAnalyticsView.enabled') : $t('views.AdminProductAnalyticsView.disabled') }}
                </span>
              </p>
            </div>
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.enforcement') }}</span>
              <p class="mt-0.5">
                <span :class="store.transparency.enforcement_enabled ? 'badge badge-context-purple' : 'badge badge-status-muted'" data-testid="enforcement-enabled">
                  {{ store.transparency.enforcement_enabled ? $t('views.AdminProductAnalyticsView.active') : $t('views.AdminProductAnalyticsView.inactive') }}
                </span>
              </p>
            </div>
          </div>
        </SectionCard>
      </div>
    </template>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import PageHeader from '../components/shared/PageHeader.vue'
import SectionCard from '../components/shared/SectionCard.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { useProductAnalyticsStore } from '../stores/productAnalyticsStore'
import { formatDateShortWithTime } from '../lib/formatDate'
import { useI18n } from 'vue-i18n'

const store = useProductAnalyticsStore()
const { t } = useI18n()

const consentLevelLabel = computed(() => {
  const map: Record<string, string> = {
    all: t('views.AdminProductAnalyticsView.level_all'),
    off: t('views.AdminProductAnalyticsView.level_off'),
  }
  return map[store.transparency?.consent_level ?? ''] || store.transparency?.consent_level || t('views.AdminProductAnalyticsView.level_unknown')
})

onMounted(() => {
  store.fetchTransparency()
})

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  if (Number.isNaN(d.getTime())) return '—'
  return formatDateShortWithTime(d)
}
</script>
