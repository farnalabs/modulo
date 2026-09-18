<template>
  <div class="page-wide">
    <FeatureGate feature-name="environment_profiles" required-tier="team" show-disabled>
      <PageHeader
        :title="$t('views.AdminRunnersView.runners')"
        :subtitle="$t('views.AdminRunnersView.subtitle')"
      />

      <PageTabs :tabs="tabs" />

      <ErrorAlert
        v-if="statusError"
        class="mb-6"
        :message="statusError"
        :on-retry="reloadStatus"
        data-testid="runner-status-error"
      />

      <RunnerStatusStrip :status="status" :errored="statusErrored" class="mb-6" />

      <RouterView v-slot="{ Component }">
        <component :is="Component" :status="status" :reload-status="reloadStatus" />
      </RouterView>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import PageTabs from '../components/PageTabs.vue'
import RunnerStatusStrip from '../components/runners/RunnerStatusStrip.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { useDataFetch } from '../composables/useDataFetch'
import { api } from '../lib/api/client'
import type { RunnersStatus } from '../lib/runnersStatus'

const { t } = useI18n()

const tabs = [
  { label: t('views.AdminRunnersView.tab_profiles'), to: '/admin/runners/profiles' },
  { label: t('views.AdminRunnersView.tab_concurrency'), to: '/admin/runners/concurrency' },
]

const { data: status, error: statusFetchError, load: reloadStatus } = useDataFetch<RunnersStatus>(
  () => api.GET('/api/v1/runners/status'),
  // FAR-591 D5 (qa F12): the strip refetches on the health probe's own
  // cadence — one cheap org-indexed cache read, never an engine probe.
  { refetchInterval: 60_000 },
)

const statusError = computed(() =>
  statusFetchError.value ? t('views.RunnersStatusStrip.fetch_failed') : null,
)
const statusErrored = computed(() => statusFetchError.value !== null)
</script>
