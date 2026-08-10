<template>
  <FeatureGate feature-name="runtime_config" required-tier="team" show-disabled>
  <div class="page-wide">
    <header class="flex items-center justify-between">
      <PageHeader :title="$t('views.AdminSystemConfigView.system_admin_config')" :subtitle="$t('views.AdminSystemConfigView.deploymentwide_system_configuration_system_admin_only')" />
      <button
        type="button"
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
        :disabled="loading"
        @click="loadConfig"
      >
        Refresh
      </button>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadConfig" />

    <div v-else-if="items.length === 0" class="rounded-lg border p-6 text-center text-sm text-muted-foreground">
      No configuration entries found.
    </div>

    <div v-else class="rounded-lg border">
      <table class="w-full">
        <thead>
          <tr class="border-b text-left text-sm font-medium text-muted-foreground">
            <th class="px-4 py-3">{{ $t('views.AdminSystemConfigView.key') }}</th>
            <th class="px-4 py-3">{{ $t('views.AdminSystemConfigView.value') }}</th>
            <th class="px-4 py-3">{{ $t('views.AdminSystemConfigView.updated') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="entry in items"
            :key="entry.key"
            class="border-b last:border-0 hover:bg-muted/50 transition-colors"
          >
            <td class="px-4 py-3">
              <code class="text-sm font-mono">{{ entry.key }}</code>
            </td>
            <td class="px-4 py-3">
              <JsonViewer :data="entry.value" :show-toolbar="true" :max-height="'20rem'" />
            </td>
            <td class="px-4 py-3 text-sm text-muted-foreground">
              {{ entry.updated_at || '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { Ref, ref, watch } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

interface ConfigEntry {
  key: string
  value: unknown
  updated_at: string | null
}

const { loading, error, data, load: loadConfig } = useDataFetch(
  () => api.GET('/api/v1/system-admin/config'),
)

const items: Ref<ConfigEntry[]> = ref([])
watch(data, (d) => {
  if (d) items.value = d as unknown as ConfigEntry[]
}, { immediate: true })
</script>
