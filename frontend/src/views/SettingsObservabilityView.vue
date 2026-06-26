<template>
  <div class="mx-auto max-w-4xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Observability</h1>
      <p class="mt-1 text-muted-foreground">Configure OpenTelemetry export and LangSmith integration</p>
    </header>

    <div v-if="envOverrideActive" class="rounded-lg border border-amber-500/50 bg-amber-50 p-4 text-sm text-amber-800">
      <p class="font-medium">Environment variable override active</p>
      <p class="mt-1">
        The environment variable <code class="rounded bg-amber-100 px-1 py-0.5 text-xs">OTEL_EXPORTER_OTLP_ENDPOINT</code>
        is set to <strong>{{ effectiveOtlpEndpoint }}</strong>. Changes made here will apply when the environment variable is unset.
      </p>
    </div>

    <div v-if="loading" class="flex items-center justify-center py-16">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="loadError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
      {{ loadError }}
      <button class="ml-2 underline" @click="loadSettings">Retry</button>
    </div>

    <form v-else @submit.prevent="saveSettings" class="space-y-6">
      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-lg font-semibold">OTLP Endpoint</h2>
        <div>
          <label class="mb-1 block text-sm font-medium">Endpoint URL</label>
          <input
            v-model="otlpEndpoint"
            type="url"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="https://otlp.example.com:4318"
          />
        </div>
      </div>

      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <div class="mb-4 flex items-center justify-between">
          <h2 class="text-lg font-semibold">OTLP Headers</h2>
          <button
            type="button"
            class="rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
            @click="addHeader"
          >
            Add Header
          </button>
        </div>
        <div v-if="otlpHeaders.length === 0" class="text-sm text-muted-foreground">
          No custom headers configured.
        </div>
        <div v-for="(header, index) in otlpHeaders" :key="index" class="mb-2 flex items-center gap-2">
          <input
            v-model="header.key"
            type="text"
            class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="Header name"
          />
          <input
            v-model="header.value"
            type="text"
            class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="Header value"
          />
          <button
            type="button"
            class="rounded p-1 text-destructive hover:bg-destructive/10"
            title="Remove header"
            @click="removeHeader(index)"
          >
            <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M18 6 6 18" /><path d="m6 6 12 12" />
            </svg>
          </button>
        </div>
      </div>

      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-lg font-semibold">Export Interval</h2>
        <div>
          <label class="mb-1 block text-sm font-medium">Interval (seconds)</label>
          <input
            v-model.number="exportIntervalSeconds"
            type="number"
            min="1"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <p class="mt-1 text-xs text-muted-foreground">How frequently telemetry data is exported. Minimum 1 second.</p>
        </div>
      </div>

      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-lg font-semibold">LangSmith</h2>
        <div class="space-y-4">
          <div class="flex items-center gap-3">
            <button
              type="button"
              class="relative inline-flex h-6 w-11 cursor-pointer items-center"
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
            <span class="text-sm font-medium">Enable LangSmith tracing</span>
          </div>
          <div v-if="langsmithEnabled">
            <label class="mb-1 block text-sm font-medium">API Key</label>
            <input
              v-model="langsmithApiKey"
              :type="showLangsmithKey ? 'text' : 'password'"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="hasLangsmithKey ? 'Leave blank to keep existing key' : 'Enter LangSmith API key'"
            />
            <div class="mt-1 flex items-center gap-2">
              <button
                type="button"
                class="text-xs text-muted-foreground hover:text-foreground"
                @click="showLangsmithKey = !showLangsmithKey"
              >
                {{ showLangsmithKey ? 'Hide' : 'Show' }}
              </button>
              <span v-if="hasLangsmithKey && !langsmithApiKey" class="text-xs text-muted-foreground">A key is already stored.</span>
            </div>
          </div>
        </div>
      </div>

      <div v-if="formError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
        {{ formError }}
      </div>
      <div v-if="formSuccess" class="rounded-lg border border-green-500/50 bg-green-50 p-4 text-sm text-green-800">
        {{ formSuccess }}
      </div>

      <div
        v-if="testResult"
        class="rounded-lg border p-4 text-sm"
        :class="testResult.success ? 'border-green-500/50 bg-green-50 text-green-800' : 'border-destructive/50 bg-destructive/10 text-destructive'"
      >
        <p class="font-medium">{{ testResult.success ? 'Connection successful' : 'Connection failed' }}</p>
        <p class="mt-1">{{ testResult.message }}</p>
      </div>

      <div class="flex items-center gap-3 pt-2">
        <button
          type="button"
          :disabled="testing"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          @click="testConnection"
        >
          {{ testing ? 'Testing...' : 'Test Connection' }}
        </button>
        <div class="flex-1" />
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
          @click="resetForm"
        >
          Reset
        </button>
        <button
          type="submit"
          :disabled="saving"
          class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {{ saving ? 'Saving...' : 'Save' }}
        </button>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'

type OtelSettingsResponse = components['schemas']['OtelSettingsResponse']
type TestSpanResult = components['schemas']['TestSpanResult']

interface HeaderRow {
  key: string
  value: string
}

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

const savedOtlpHeaders = ref<Record<string, string>>({})

function headersToRows(headers: Record<string, string>): HeaderRow[] {
  return Object.entries(headers).map(([key, value]) => ({ key, value }))
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
      loadError.value = `Failed to load settings: ${err}`
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
    loadError.value = `Failed to load settings: ${e instanceof Error ? e.message : String(e)}`
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
      formError.value = `Save failed: ${err}`
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
      formSuccess.value = 'Settings saved successfully.'
      setTimeout(() => { formSuccess.value = null }, 3000)
    }
  } catch (e: unknown) {
    formError.value = `Save failed: ${e instanceof Error ? e.message : String(e)}`
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
      setTimeout(() => { testResult.value = null }, 10000)
    }
  } catch (e: unknown) {
    testResult.value = { success: false, message: e instanceof Error ? e.message : String(e) }
  } finally {
    testing.value = false
  }
}

onMounted(loadSettings)
</script>
