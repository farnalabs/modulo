<template>
  <div class="mx-auto max-w-4xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.SettingsObservabilityView.observability') }}</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.SettingsObservabilityView.configure_opentelemetry_export_and_langsmith_integration') }}</p>
    </header>

    <FeatureGate feature-name="observability" required-tier="team" show-disabled>

      <div v-if="envOverrideActive" data-testid="settings-observability-env-override" class="rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm text-warning">
        <p class="font-medium">{{ $t('views.SettingsObservabilityView.env_override_active') }}</p>
        <p class="mt-1">
          {{ $t('views.SettingsObservabilityView.env_override_description_prefix') }}
          <code class="rounded bg-warning/10 px-1 py-0.5 text-xs">OTEL_EXPORTER_OTLP_ENDPOINT</code>
          {{ $t('views.SettingsObservabilityView.env_override_description_mid') }}
          <strong>{{ effectiveOtlpEndpoint }}</strong>.
          {{ $t('views.SettingsObservabilityView.env_override_description_suffix') }}
        </p>
      </div>

      <LoadingSpinner v-if="loading" data-testid="settings-observability-loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadSettings" />

      <form v-else @submit.prevent="saveSettings" class="space-y-6">
        <div class="rounded-lg border bg-card p-6 shadow-sm">
          <h2 class="mb-4 text-lg font-semibold">{{ $t('views.SettingsObservabilityView.otlp_endpoint') }}</h2>
          <div>
            <label class="mb-1 block text-sm font-medium">{{ $t('views.SettingsObservabilityView.endpoint_url') }}</label>
            <input
              v-model="otlpEndpoint"
              type="url"
              data-testid="settings-observability-otlp-endpoint"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              placeholder="https://otlp.example.com:4318"
            />
          </div>
        </div>

        <div class="rounded-lg border bg-card p-6 shadow-sm">
          <div class="mb-4 flex items-center justify-between">
            <h2 class="text-lg font-semibold">{{ $t('views.SettingsObservabilityView.otlp_headers') }}</h2>
            <button
              type="button"
              class="rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
              data-testid="settings-observability-add-header"
              @click="addHeader"
            >
              {{ $t('views.SettingsObservabilityView.add_header') }}
            </button>
          </div>
          <div v-if="otlpHeaders.length === 0" data-testid="settings-observability-no-headers" class="text-sm text-muted-foreground">
            {{ $t('views.SettingsObservabilityView.no_custom_headers_configured') }}
          </div>
          <div v-for="(header, index) in otlpHeaders" :key="index" class="mb-2 flex items-center gap-2">
            <input
              v-model="header.key"
              type="text"
              data-testid="settings-observability-header-key"
              class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="$t('views.SettingsObservabilityView.header_name')"
            />
            <input
              v-model="header.value"
              type="text"
              data-testid="settings-observability-header-value"
              class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="$t('views.SettingsObservabilityView.header_value')"
            />
            <button
              type="button"
              class="rounded p-1 text-destructive hover:bg-destructive/10"
              data-testid="settings-observability-remove-header"
              :aria-label="$t('views.SettingsObservabilityView.remove_header')"
              :title="$t('views.SettingsObservabilityView.remove_header')"
              @click="removeHeader(index)"
            >
              <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M18 6 6 18" /><path d="m6 6 12 12" />
              </svg>
            </button>
          </div>
        </div>

        <div class="rounded-lg border bg-card p-6 shadow-sm">
          <h2 class="mb-4 text-lg font-semibold">{{ $t('views.SettingsObservabilityView.export_interval') }}</h2>
          <div>
            <label class="mb-1 block text-sm font-medium">{{ $t('views.SettingsObservabilityView.interval_seconds') }}</label>
            <input
              v-model.number="exportIntervalSeconds"
              type="number"
              min="1"
              data-testid="settings-observability-export-interval"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.SettingsObservabilityView.export_interval_description') }}</p>
          </div>
        </div>

        <div class="rounded-lg border bg-card p-6 shadow-sm">
          <h2 class="mb-4 text-lg font-semibold">{{ $t('views.SettingsObservabilityView.langsmith') }}</h2>
          <div class="space-y-4">
            <div class="flex items-center gap-3">
              <button
                type="button"
                class="relative inline-flex h-6 w-11 cursor-pointer items-center"
                data-testid="settings-observability-langsmith-toggle"
                :aria-label="$t('views.SettingsObservabilityView.toggle_langsmith')"
                @click="langsmithEnabled = !langsmithEnabled"
              >
                <div
                  class="h-6 w-11 rounded-full transition-colors"
                  :class="langsmithEnabled ? 'bg-primary' : 'bg-input'"
                >
                  <div
                    class="h-5 w-5 translate-x-0.5 rounded-full bg-white shadow-sm transition-transform"
                    :class="langsmithEnabled ? 'translate-x-[1.375rem]' : ''"
                    style="margin-top: 2px;"
                  />
                </div>
              </button>
              <span class="text-sm font-medium">{{ $t('views.SettingsObservabilityView.enable_langsmith_tracing') }}</span>
            </div>
            <div v-if="langsmithEnabled">
              <label class="mb-1 block text-sm font-medium">{{ $t('views.SettingsObservabilityView.api_key') }}</label>
              <textarea
                v-model="langsmithApiKey"
                data-testid="settings-observability-langsmith-api-key"
                rows="3"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :placeholder="hasLangsmithKey ? $t('views.SettingsObservabilityView.leave_blank_to_keep_existing_key') : $t('views.SettingsObservabilityView.enter_langsmith_api_key')"
              />
              <div class="mt-1 flex items-center gap-2">
                <button
                  type="button"
                  class="text-xs text-muted-foreground hover:text-foreground"
                  data-testid="settings-observability-toggle-key-visibility"
                  @click="showLangsmithKey = !showLangsmithKey"
                >
                  {{ showLangsmithKey ? $t('views.SettingsObservabilityView.hide') : $t('views.SettingsObservabilityView.show') }}
                </button>
                <span v-if="hasLangsmithKey && !langsmithApiKey" class="text-xs text-muted-foreground">{{ $t('views.SettingsObservabilityView.key_already_stored') }}</span>
              </div>
            </div>
          </div>
        </div>

        <div v-if="formError" data-testid="settings-observability-form-error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
          {{ formError }}
        </div>
        <div v-if="formSuccess" data-testid="settings-observability-form-success" class="rounded-lg border border-success/50 bg-success/10 p-4 text-sm text-success">
          {{ formSuccess }}
        </div>

        <div
          v-if="testResult"
          data-testid="settings-observability-test-result"
          class="rounded-lg border p-4 text-sm"
          :class="testResult.success ? 'border-success/50 bg-success/10 text-success' : 'border-destructive/50 bg-destructive/10 text-destructive'"
        >
          <p class="font-medium">{{ testResult.success ? $t('views.SettingsObservabilityView.connection_successful') : $t('views.SettingsObservabilityView.connection_failed') }}</p>
          <p class="mt-1">{{ testResult.message }}</p>
        </div>

        <div class="flex items-center gap-3 pt-2">
          <button
            type="button"
            :disabled="testing"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
            data-testid="settings-observability-test-connection"
            @click="testConnection"
          >
            {{ testing ? $t('views.SettingsObservabilityView.testing') : $t('views.SettingsObservabilityView.test_connection') }}
          </button>
          <div class="flex-1" />
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="settings-observability-reset"
            @click="resetForm"
          >
            {{ $t('views.SettingsObservabilityView.reset') }}
          </button>
          <button
            type="submit"
            :disabled="saving"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            data-testid="settings-observability-save"
          >
            {{ saving ? $t('views.SettingsObservabilityView.saving') : $t('views.SettingsObservabilityView.save') }}
          </button>
        </div>
      </form>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import { formatApiError } from '../lib/api/formatError'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

type OtelSettingsResponse = components['schemas']['OtelSettingsResponse']
type TestSpanResult = components['schemas']['TestSpanResult']

interface HeaderRow {
  key: string
  value: string
}

const planStore = usePlanStore()
const { t } = useI18n()

const loading = ref(true)
const loadError = ref<string | null>(null)

const otlpEndpoint = ref('')
const otlpHeaders = ref<HeaderRow[]>([])
const exportIntervalSeconds = ref(10)
const langsmithEnabled = ref(false)
const langsmithApiKey = ref('')
const showLangsmithKey = ref(false)
const envOverrideActive = ref(false)
const effectiveOtlpEndpoint = ref('')
const hasLangsmithKey = ref(false)

const saving = ref(false)
const formError = ref<string | null>(null)
const formSuccess = ref<string | null>(null)

const testing = ref(false)
const testResult = ref<TestSpanResult | null>(null)
let observabilityFormTimeout: ReturnType<typeof setTimeout> | null = null
let observabilityTestTimeout: ReturnType<typeof setTimeout> | null = null

const savedOtlpHeaders = ref<Record<string, string>>({})

function headersToRows(headers: Record<string, string> | undefined | null): HeaderRow[] {
  return Object.entries(headers ?? {}).map(([key, value]) => ({ key, value }))
}

function rowsToHeaders(rows: HeaderRow[]): Record<string, string> {
  const result: Record<string, string> = {}
  for (const row of rows) {
    const k = row.key.trim()
    if (!k) continue
    const v = row.value
    if (v === '••••••' && savedOtlpHeaders.value[k] !== undefined) {
      result[k] = savedOtlpHeaders.value[k]
    } else {
      result[k] = v
    }
  }
  return result
}

async function loadSettings() {
  loading.value = true
  loadError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/settings/observability')
    if (err) {
      loadError.value = t('views.SettingsObservabilityView.failed_to_load_settings') + ' ' + formatApiError(err)
    } else if (data) {
      const s = data as unknown as OtelSettingsResponse
      otlpEndpoint.value = s.otlp_endpoint
      savedOtlpHeaders.value = { ...s.otlp_headers }
      otlpHeaders.value = headersToRows(s.otlp_headers)
      exportIntervalSeconds.value = s.export_interval_seconds
      langsmithEnabled.value = s.langsmith_enabled
      hasLangsmithKey.value = s.has_langsmith_api_key
      envOverrideActive.value = s.env_override_active
      effectiveOtlpEndpoint.value = s.effective_otlp_endpoint
    }
  } catch (e: unknown) {
    loadError.value = t('views.SettingsObservabilityView.failed_to_load_settings') + ' ' + formatApiError(e)
  } finally {
    loading.value = false
  }
}

function resetForm() {
  formError.value = null
  formSuccess.value = null
  testResult.value = null
  loadSettings()
}

function addHeader() {
  otlpHeaders.value.push({ key: '', value: '' })
}

function removeHeader(index: number) {
  otlpHeaders.value.splice(index, 1)
}

async function saveSettings() {
  saving.value = true
  formError.value = null
  formSuccess.value = null

  if (otlpEndpoint.value) {
    try {
      new URL(otlpEndpoint.value)
    } catch {
      formError.value = t('views.SettingsObservabilityView.invalid_endpoint_url_please_enter_a_valid_url_eg_httpsotlpex')
      saving.value = false
      return
    }
  }

  if (exportIntervalSeconds.value < 1) {
    formError.value = t('views.SettingsObservabilityView.export_interval_must_be_at_least_1_second')
    saving.value = false
    return
  }

  try {
    const headers = rowsToHeaders(otlpHeaders.value)
    const body: Record<string, unknown> = {}
    if (otlpEndpoint.value !== '') {
      body.otlp_endpoint = otlpEndpoint.value
    }
    body.otlp_headers = headers
    body.export_interval_seconds = exportIntervalSeconds.value
    body.langsmith_enabled = langsmithEnabled.value
    if (langsmithApiKey.value) {
      body.langsmith_api_key = langsmithApiKey.value
    } else if (!hasLangsmithKey.value) {
      body.langsmith_api_key = ''
    }
    const { data, error: err } = await api.PUT('/api/v1/settings/observability', {
      body: body as components['schemas']['OtelSettingsUpdate'],
    })
    if (err) {
      formError.value = t('views.SettingsObservabilityView.save_failed') + ' ' + formatApiError(err)
    } else if (data) {
      const s = data as unknown as OtelSettingsResponse
      otlpEndpoint.value = s.otlp_endpoint
      savedOtlpHeaders.value = { ...s.otlp_headers }
      otlpHeaders.value = headersToRows(s.otlp_headers)
      exportIntervalSeconds.value = s.export_interval_seconds
      langsmithEnabled.value = s.langsmith_enabled
      hasLangsmithKey.value = s.has_langsmith_api_key
      langsmithApiKey.value = ''
      envOverrideActive.value = s.env_override_active
      effectiveOtlpEndpoint.value = s.effective_otlp_endpoint
      formSuccess.value = t('views.SettingsObservabilityView.settings_saved_successfully')
      if (observabilityFormTimeout) clearTimeout(observabilityFormTimeout)
      observabilityFormTimeout = setTimeout(() => { formSuccess.value = null }, 3000)
    }
  } catch (e: unknown) {
    formError.value = t('views.SettingsObservabilityView.save_failed') + ' ' + formatApiError(e)
  } finally {
    saving.value = false
  }
}

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const headers = rowsToHeaders(otlpHeaders.value)
    const { data, error: err } = await api.POST('/api/v1/settings/observability/test', {
      body: { otlp_endpoint: otlpEndpoint.value, otlp_headers: headers },
    })
    if (err) {
      testResult.value = { success: false, message: String(err) }
    } else if (data) {
      testResult.value = data as unknown as TestSpanResult
      if (observabilityTestTimeout) clearTimeout(observabilityTestTimeout)
      observabilityTestTimeout = setTimeout(() => { testResult.value = null }, 10000)
    }
  } catch (e: unknown) {
    testResult.value = { success: false, message: e instanceof Error ? e.message : String(e) }
  } finally {
    testing.value = false
  }
}

onBeforeUnmount(() => {
  if (observabilityFormTimeout) clearTimeout(observabilityFormTimeout)
  if (observabilityTestTimeout) clearTimeout(observabilityTestTimeout)
})

onMounted(() => {
  planStore.fetchPlan()
  loadSettings()
})
</script>
