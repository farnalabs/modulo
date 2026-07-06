<template>
  <FeatureGate feature-name="error_tracking" required-tier="community" show-disabled>
    <template #locked>
      <div class="flex items-center justify-center h-64 text-muted-foreground">
        {{ $t('views.SettingsMonitorConfigView.feature_locked') }}
      </div>
    </template>
    <template #default>
      <div class="max-w-3xl mx-auto space-y-8 p-6">
        <h1 class="text-2xl font-semibold">{{ $t('views.SettingsMonitorConfigView.browser_monitoring') }}</h1>
        <p class="text-muted-foreground text-sm">
          {{ $t('views.SettingsMonitorConfigView.description') }}
        </p>

        <div v-if="loading" class="flex items-center justify-center h-32">
          <span class="animate-spin h-5 w-5 border-2 border-primary border-t-transparent rounded-full" />
        </div>

        <template v-else>
          <div class="space-y-6">
            <div
              v-for="b in backendForms"
              :key="b.key"
              class="border rounded-lg p-5 space-y-4"
              :class="{ 'opacity-50': !b.enabled }"
            >
              <div class="flex items-center justify-between">
                <div>
                  <h3 class="font-medium">{{ b.label }}</h3>
                  <p class="text-xs text-muted-foreground">{{ b.description }}</p>
                  <p v-if="!b.enabled" class="text-xs text-muted-foreground/60 mt-1">{{ b.hint }}</p>
                </div>
                <label class="relative inline-flex items-center cursor-pointer">
                  <input type="checkbox" v-model="b.enabled" class="sr-only peer" @change="onDirty" />
                  <div class="w-9 h-5 bg-muted rounded-full peer peer-checked:bg-primary peer-focus:ring-2 peer-focus:ring-primary/20 after:content-[''] after:absolute after:top-0.5 after:start-0.5 after:bg-background after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-full" />
                </label>
              </div>

              <div v-if="b.enabled" class="space-y-3">
                <div v-for="field in b.fields" :key="field.key">
                  <label class="block text-xs text-muted-foreground mb-1">{{ field.label }}</label>
                  <input
                    v-model="field.value"
                    :type="field.secret && !field.revealed ? 'password' : 'text'"
                    :placeholder="field.placeholder"
                    class="w-full px-3 py-2 text-sm border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-primary/20"
                    @input="onDirty"
                  />
                  <div v-if="field.secret" class="mt-1">
                    <button class="text-xs text-muted-foreground hover:text-foreground" @click="field.revealed = !field.revealed">
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
            <Button variant="outline" :disabled="!dirty" @click="reset">
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
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { getErrorTracker } from '../lib/error-tracking'
import { loadBackends } from '../monitor'
import type { MonitorConfig } from '../monitor/types'
import { Button } from '../components/ui/button'

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

const loading = ref(true)
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
  let perBackend: Record<string, Record<string, string>> = {}
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
    sentry: activeKeys.includes('sentry') ? (perBackend.sentry ?? { dsn: '' }) : undefined,
    'datadog-rum': activeKeys.includes('datadog_rum') ? (perBackend.datadog_rum ?? { clientToken: '' }) : undefined,
    'grafana-faro': activeKeys.includes('grafana_faro') ? (perBackend.grafana_faro ?? { url: '' }) : undefined,
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

async function load() {
  loading.value = true
  try {
    const res = await api.GET('/api/v1/admin/monitor-config')
    if (res.data) {
      fromApiPayload(res.data as Record<string, any>)
    }
  } catch (e) {
    showFlash(`${t('common.failed_to_load')}: ${e}`, 'error')
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const apiPayload = toApiPayload()
    const res = await api.PUT('/api/v1/admin/monitor-config', { body: apiPayload as any })
    if (res.error) {
      showFlash(`${t('common.failed_to_save')}: ${(res.error as any).detail}`, 'error')
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
    showFlash(`${t('common.failed_to_save')}: ${e}`, 'error')
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
