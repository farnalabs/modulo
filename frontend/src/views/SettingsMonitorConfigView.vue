<template>
  <FeatureGate feature-name="error_tracking" show-disabled>
    <template #locked>
      <div class="flex items-center justify-center h-64 text-muted-foreground">
        {{ $t('views.SettingsMonitorConfigView.feature_locked') }}
      </div>
    </template>
    <template #default>
      <div class="page-wide">
        <PageHeader :title="$t('views.SettingsMonitorConfigView.browser_monitoring')" :subtitle="$t('views.SettingsMonitorConfigView.description')" />

        <div class="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
          {{ $t('views.SettingsMonitorConfigView.explainer') }}
        </div>

        <div v-if="loading" class="flex items-center justify-center h-32">
          <span class="animate-spin h-5 w-5 border-2 border-primary border-t-transparent rounded-full" />
        </div>

        <div v-else-if="error" class="flex flex-col items-center justify-center h-32 gap-3">
          <p class="text-destructive text-sm">{{ error || 'Failed to load monitoring configuration' }}</p>
          <Button size="small" severity="secondary" outlined @click="load()">{{ $t('views.SettingsMonitorConfigView.retry') }}</Button>
        </div>

        <template v-else>
          <div class="space-y-6">
            <div
              v-for="b in backendForms"
              :key="b.key"
              class="border rounded-lg p-5 space-y-4"
              :class="{}"
            >
              <div class="flex items-center justify-between">
                <div>
                  <h3 :id="`settingsmonitorconfigview-backend-label-${b.key}`" class="font-medium">{{ b.label }}</h3>
                  <p class="text-xs text-muted-foreground">{{ b.description }}</p>
                  <p v-if="!b.enabled" class="text-xs text-muted-foreground/60 mt-1">{{ b.hint }}</p>
                </div>
                <label :for="`settingsmonitorconfigview-toggle-${b.key}`" class="relative inline-flex items-center cursor-pointer">
                  <input :id="`settingsmonitorconfigview-toggle-${b.key}`" type="checkbox" v-model="b.enabled" class="sr-only peer" :aria-labelledby="`settingsmonitorconfigview-backend-label-${b.key}`" @change="onDirty" />
                  <div class="w-9 h-5 bg-muted rounded-full peer peer-checked:bg-primary peer-focus:ring-2 peer-focus:ring-primary/20 after:content-[''] after:absolute after:top-0.5 after:start-0.5 after:bg-background after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-full" />
                </label>
              </div>

              <div v-if="b.enabled" class="space-y-3">
                <div v-for="field in b.fields" :key="field.key">
                  <label for="settingsmonitorconfigview-field-1" class="block text-xs text-muted-foreground mb-1">{{ field.label }}</label>
                  <input id="settingsmonitorconfigview-field-1"
                    v-model="field.value"
                    :type="field.secret && !field.revealed ? 'password' : 'text'"
                    :placeholder="field.placeholder"
                    class="w-full px-3 py-2 text-sm border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-primary/20"
                    @input="onDirty"
                  />
                  <div v-if="field.secret" class="mt-1">
                    <button type="button" class="text-xs text-muted-foreground hover:text-foreground" @click="field.revealed = !field.revealed">
                      {{ field.revealed ? $t('common.hide') : $t('common.show') }}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div v-if="flash" class="p-3 rounded-md text-sm" :class="flashType === 'success' ? 'bg-green-500/10 text-green-600' : 'bg-red-500/10 text-red-600'">
            {{ flash }}
          </div>

          <div class="flex gap-3 pt-4">
            <Button :disabled="saving || !dirty" @click="save">
              <span v-if="saving" class="animate-spin h-4 w-4 border-2 border-background border-t-transparent rounded-full mr-2" />
              {{ $t('common.save') }}
            </Button>
            <Button severity="secondary" outlined :disabled="!dirty" @click="reset">
              {{ $t('common.reset') }}
            </Button>
          </div>
        </template>
      </div>
    </template>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { useDataFetch } from '../composables/useDataFetch'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { getErrorTracker } from '../lib/error-tracking'
import { loadBackends } from '../monitor'
import type { MonitorConfig } from '../monitor/types'
import PageHeader from '../components/shared/PageHeader.vue'
import Button from 'primevue/button'

const { t } = useI18n()

interface BackendField {
  key: string
  label: string
  value: string
  placeholder: string
  secret: boolean
  revealed: boolean
}

interface BackendForm {
  key: string
  label: string
  description: string
  hint: string
  enabled: boolean
  fields: BackendField[]
}

const backendForms = reactive<BackendForm[]>([
  {
    key: 'builtin',
    label: 'Built-in (DB)',
    description: 'Store errors in Modulo\'s own database. Always available, no external service needed.',
    hint: '',
    enabled: true,
    fields: [],
  },
  {
    key: 'sentry',
    label: 'Sentry',
    description: 'Error tracking with session replays, source maps, and performance monitoring.',
    hint: 'Requires a Sentry DSN from https://sentry.io',
    enabled: false,
    fields: [
      { key: 'dsn', label: 'DSN', value: '', placeholder: 'https://xxx@o123.ingest.sentry.io/123', secret: true, revealed: false },
    ],
  },
  {
    key: 'datadog_rum',
    label: 'Datadog RUM',
    description: 'Real User Monitoring with performance metrics, session replays, and logs.',
    hint: 'Requires a Datadog RUM client token — create one in Datadog under UX Monitoring',
    enabled: false,
    fields: [
      { key: 'clientToken', label: 'Client Token', value: '', placeholder: 'pub123456...', secret: true, revealed: false },
      { key: 'site', label: 'Site', value: 'datadoghq.com', placeholder: 'datadoghq.com', secret: false, revealed: false },
    ],
  },
  {
    key: 'grafana_faro',
    label: 'Grafana Faro',
    description: 'OpenTelemetry-based monitoring with Grafana Cloud. No cookies set.',
    hint: 'Requires a Faro collector URL — set up a Grafana Cloud stack with Faro',
    enabled: false,
    fields: [
      { key: 'url', label: 'Collector URL', value: '', placeholder: 'https://faro-collector.example.com', secret: false, revealed: false },
      { key: 'apiKey', label: 'API Key (optional)', value: '', placeholder: '', secret: true, revealed: false },
    ],
  },
])

const { loading, load, error } = useDataFetch(
  async () => {
    const res = await api.GET('/api/v1/admin/monitor-config')
    if (res.error) {
      if (error.value) console.warn("[SettingsMonitorConfigView] Failed to load monitor config:", error.value)
    }
    if (res.data) {
      fromApiPayload(res.data as Record<string, any>)
    }
    return res
  },
  { immediate: false }
)
const saving = ref(false)
const dirty = ref(false)
const flash = ref('')
const flashType = ref<'success' | 'error'>('success')
let flashTimer: ReturnType<typeof setTimeout> | null = null

function onDirty() {
  dirty.value = true
}

function showFlash(msg: string, type: 'success' | 'error') {
  if (flashTimer) clearTimeout(flashTimer)
  flash.value = msg
  flashType.value = type
  flashTimer = setTimeout(() => { flash.value = '' }, 4000)
}

function toMonitorConfig(): MonitorConfig {
  const activeKeys: string[] = []
  const perBackend: Record<string, Record<string, string>> = {}
  for (const b of backendForms) {
    if (b.enabled) {
      activeKeys.push(b.key)
      if (b.key !== 'builtin') {
        const cfg: Record<string, string> = {}
        for (const f of b.fields) {
          if (f.value) cfg[f.key] = f.value
        }
        if (Object.keys(cfg).length > 0) {
          perBackend[b.key] = cfg
        }
      }
    }
  }
  if (activeKeys.length === 0) activeKeys.push('builtin')

  return {
    monitorBackends: activeKeys,
    sentry: activeKeys.includes('sentry')
      ? { dsn: perBackend.sentry?.dsn ?? '' }
      : undefined,
    datadogRum: activeKeys.includes('datadog_rum')
      ? { clientToken: perBackend.datadog_rum?.clientToken ?? '' }
      : undefined,
    grafanaFaro: activeKeys.includes('grafana_faro')
      ? { url: perBackend.grafana_faro?.url ?? '' }
      : undefined,
  }
}

function toApiPayload() {
  const activeKeys: string[] = []
  const perBackend: Record<string, Record<string, string>> = {}
  for (const b of backendForms) {
    if (b.enabled) {
      const apiKey = b.key === 'datadog_rum' ? 'datadog_rum' : b.key
      activeKeys.push(apiKey)
      if (b.key !== 'builtin') {
        const cfg: Record<string, string> = {}
        for (const f of b.fields) {
          if (f.value) cfg[f.key] = f.value
        }
        if (Object.keys(cfg).length > 0) {
          perBackend[apiKey] = cfg
        }
      }
    }
  }
  if (activeKeys.length === 0) activeKeys.push('builtin')
  return { backends: activeKeys, ...perBackend }
}

function fromApiPayload(data: Record<string, any>) {
  const activeBackends: string[] = data.backends ?? ['builtin']

  for (const b of backendForms) {
    const apiKey = b.key === 'datadog_rum' ? 'datadog_rum' : b.key
    b.enabled = activeBackends.includes(apiKey) || (b.key === 'builtin' && activeBackends.length === 0)

    if (b.key !== 'builtin') {
      const cfg = data[apiKey] as Record<string, string> | undefined
      for (const f of b.fields) {
        f.value = cfg?.[f.key] ?? ''
        f.revealed = false
      }
    }
  }

  dirty.value = false
}

async function save() {
  saving.value = true
  try {
    const apiPayload = toApiPayload()
    const res = await api.PUT('/api/v1/admin/monitor-config', { body: apiPayload as any })
    if (res.error) {
      showFlash(`${t('common.failed_to_save')}: ${formatApiError(res.error)}`, 'error')
      return
    }

    const monitorConfig = toMonitorConfig()
    const activeBackends = await loadBackends(monitorConfig)
    const tracker = getErrorTracker()
    if (tracker) {
      tracker.reloadBackends(activeBackends)
    }

    fromApiPayload(res.data as Record<string, any>)
    showFlash(t('common.configuration_saved'), 'success')
  } catch (e) {
    showFlash(`${t('common.failed_to_save')}: ${formatApiError(e)}`, 'error')
  } finally {
    saving.value = false
  }
}

function reset() {
  load()
}

onMounted(load)

onBeforeUnmount(() => {
  if (flashTimer) clearTimeout(flashTimer)
})
</script>
