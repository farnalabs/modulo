<template>
  <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.SettingsRateLimitsView.rate_limits')" data-test-id="rate-limits-title" :subtitle="$t('views.SettingsRateLimitsView.view_perroute_rate_limiting_rules_and_current_usage')" />

    <FeatureGate feature-name="rate_limits" required-tier="team" show-disabled>

    <div v-if="loading" class="space-y-6">
      <div class="card p-6 animate-pulse">
        <div class="mb-4 flex items-center justify-between">
          <div class="h-5 w-24 bg-muted rounded" />
          <div class="h-5 w-20 bg-muted rounded" />
        </div>
        <div class="h-4 w-64 bg-muted rounded" />
      </div>
      <div class="card p-6 animate-pulse">
        <div class="mb-4 h-5 w-20 bg-muted rounded" />
        <div class="h-4 w-full bg-muted rounded" />
        <div class="mt-3 h-4 w-3/4 bg-muted rounded" />
        <div class="mt-3 h-4 w-1/2 bg-muted rounded" />
      </div>
    </div>

    <ErrorAlert v-else-if="loadError && !featureRequired" :message="loadError" :on-retry="loadRules" />

    <div v-else-if="!featureRequired" class="space-y-6">
      <div data-testid="rate-limits-mode" class="rounded-lg border bg-card p-6 shadow-sm">
        <div class="mb-4 flex items-center justify-between">
          <h2 class="text-base font-semibold">{{ $t('views.SettingsRateLimitsView.mode') }}</h2>
          <span
            class="rounded-full px-3 py-1 text-xs font-medium"
            :class="mode === 'redis' ? 'badge badge-status-success' : 'badge badge-status-warning'"
          >
            {{ mode === 'redis' ? $t('views.SettingsRateLimitsView.redis') : $t('views.SettingsRateLimitsView.inmemory') }}
          </span>
        </div>
        <p class="text-sm text-muted-foreground">
          {{ mode === 'redis' ? $t('views.SettingsRateLimitsView.rate_limiting_is_backed_by_redis') : $t('views.SettingsRateLimitsView.rate_limiting_uses_inmemory_token_buckets_redis_not_configur') }}
        </p>
      </div>

      <div data-testid="rate-limits-rules" class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.SettingsRateLimitsView.rules') }}</h2>
        <EmptyState
          v-if="rules.length === 0"
          :title="$t('views.SettingsRateLimitsView.no_rate_limit_rules_configured')"
        />
        <div v-else class="overflow-x-auto">
          <table data-testid="rate-limits-table" class="w-full text-sm">
          <thead>
            <tr class="border-b text-left text-muted-foreground">
              <th class="pb-2 font-medium">{{ $t('views.SettingsRateLimitsView.path_prefix') }}</th>
              <th class="pb-2 font-medium">{{ $t('views.SettingsRateLimitsView.max_requests') }}</th>
              <th class="pb-2 font-medium">{{ $t('views.SettingsRateLimitsView.window_s') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="rule in rules" :key="rule.path_prefix" class="border-b last:border-b-0">
              <td class="py-3 font-mono text-xs">{{ rule.path_prefix }}</td>
              <td class="py-3">{{ rule.max_requests }}</td>
              <td class="py-3">{{ rule.window_s }}</td>
            </tr>
          </tbody>
        </table>
        </div>
      </div>
    </div>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import PageHeader from '../components/shared/PageHeader.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'

function isFeatureRequiredError(err: unknown): boolean {
  if (typeof err !== 'object' || err === null) return false
  const obj = err as Record<string, unknown>
  return obj.status === 402 || obj.type === 'urn:problem:modulo:feature_required'
}

const featureRequired = ref(false)
const { loading, error: loadError, data, load: loadRules } = useDataFetch(
  async () => {
    const res = await api.GET('/api/v1/admin/rate-limits')
    if (res.error) {
      if (isFeatureRequiredError(res.error)) {
        featureRequired.value = true
        return { data: { mode: 'in_memory', rules: [] }, error: undefined }
      }
      featureRequired.value = false
      return { data: undefined, error: { detail: formatApiError(res.error) } }
    }
    featureRequired.value = false
    return res
  },
)
const mode = computed(() => data.value?.mode ?? 'in_memory')
const rules = computed(() => data.value?.rules ?? [])
</script>
