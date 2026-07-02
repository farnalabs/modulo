<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.AdminSystemConfigView.system_admin_config') }}</h1>
        <p class="mt-1 text-muted-foreground">{{ $t('views.AdminSystemConfigView.deploymentwide_system_configuration_system_admin_only') }}</p>
      </div>
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
            <th class="px-4 py-3">Key</th>
            <th class="px-4 py-3">Value</th>
            <th class="px-4 py-3">Updated</th>
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
              <code class="text-sm font-mono break-all max-w-xs inline-block">
                {{ formatValue(entry.value) }}
              </code>
            </td>
            <td class="px-4 py-3 text-sm text-muted-foreground">
              {{ entry.updated_at || '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError, type ProblemDetail } from '../lib/api/formatError'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

interface ConfigEntry {
  key: string
  value: unknown
  updated_at: string | null
}

const loading = ref(true)
const error = ref<string | null>(null)
const items = ref<ConfigEntry[]>([])

function formatValue(value: unknown): string {
  if (value === null) return '(null)'
  if (value === undefined) return '(undefined)'
  if (typeof value === 'string') return value
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

async function loadConfig() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/system-admin/config')
    if (err) {
      error.value = err && typeof err === 'object' && 'detail' in err
        ? `Failed to load system config: ${(err as ProblemDetail).detail}`
        : `Failed to load system config: ${formatApiError(err)}`
    } else if (data) {
      items.value = data as unknown as ConfigEntry[]
    }
  } catch (e: unknown) {
    error.value = `Failed to load system config: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

onMounted(loadConfig)
</script>
