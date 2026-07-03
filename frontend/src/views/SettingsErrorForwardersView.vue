<template>
  <div data-theme="agent" class="mx-auto max-w-4xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.SettingsErrorForwardersView.error_forwarders') }}</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.SettingsErrorForwardersView.configure_external_error_tracking_and_alerting_integrations') }}</p>
    </header>

    <FeatureGate feature-name="error_forwarders" required-tier="team" show-disabled>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadForwarders" />

      <div v-else class="space-y-6">
        <div v-for="fwd in forwarders" :key="fwd.forwarder_type" class="rounded-lg border bg-card shadow-sm">
          <div class="flex items-center justify-between p-4">
            <div class="flex items-center gap-3">
              <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-muted">
                <span class="text-lg font-bold text-muted-foreground">{{ fwd.display_name.charAt(0) }}</span>
              </div>
              <div>
                <h3 class="font-semibold">{{ fwd.display_name }}</h3>
                <div class="flex items-center gap-2 mt-0.5">
                  <span
                    class="inline-block h-2 w-2 rounded-full"
                    :class="fwd.last_test_ok === true ? 'bg-green-500' : fwd.last_test_ok === false ? 'bg-red-500' : 'bg-gray-400'"
                    :title="fwd.last_test_ok === true ? 'Last test passed' : fwd.last_test_ok === false ? 'Last test failed' : 'Not tested'"
                  />
                  <span class="text-xs text-muted-foreground">
                    {{ fwd.last_test_ok === true ? 'Connected' : fwd.last_test_ok === false ? 'Failed' : 'Not tested' }}
                  </span>
                </div>
              </div>
            </div>
            <div class="flex items-center gap-3">
              <span
                v-if="!fwd.configured"
                class="rounded-full bg-amber-500/10 px-2.5 py-0.5 text-xs font-medium text-amber-600"
              >Not configured</span>
              <button
                type="button"
                class="relative inline-flex h-6 w-11 cursor-pointer items-center"
                :aria-label="'Toggle ' + fwd.display_name"
                @click="toggleForwarder(fwd)"
              >
                <div
                  class="h-6 w-11 rounded-full transition-colors"
                  :class="fwd.enabled ? 'bg-primary' : 'bg-input'"
                >
                  <div
                    class="h-5 w-5 translate-x-0.5 rounded-full bg-white shadow-sm transition-transform"
                    :class="fwd.enabled ? 'translate-x-[1.375rem]' : ''"
                    style="margin-top: 2px;"
                  />
                </div>
              </button>
            </div>
          </div>

          <div
            v-if="expanded[fwd.forwarder_type]"
            class="border-t px-4 py-4 space-y-4"
          >
            <!-- Sentry -->
            <template v-if="fwd.forwarder_type === 'sentry'">
              <div>
                <label class="mb-1 block text-sm font-medium">DSN</label>
                <input
                  v-model="configs.sentry.dsn"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="https://key@sentry.io/123"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Org Slug</label>
                <input
                  v-model="configs.sentry.org_slug"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="my-org"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Project Slug</label>
                <input
                  v-model="configs.sentry.project_slug"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="my-project"
                />
              </div>
            </template>

            <!-- DataDog -->
            <template v-if="fwd.forwarder_type === 'datadog'">
              <div>
                <label class="mb-1 block text-sm font-medium">API Key</label>
                <input
                  v-model="configs.datadog.api_key"
                  type="password"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="Enter DataDog API key"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Site</label>
                <select
                  v-model="configs.datadog.site"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <option value="datadoghq.com">US (datadoghq.com)</option>
                  <option value="datadoghq.eu">EU (datadoghq.eu)</option>
                  <option value="us3.datadoghq.com">US3 (us3.datadoghq.com)</option>
                  <option value="us5.datadoghq.com">US5 (us5.datadoghq.com)</option>
                  <option value="ddog-gov.com">Gov (ddog-gov.com)</option>
                </select>
              </div>
            </template>

            <!-- PagerDuty -->
            <template v-if="fwd.forwarder_type === 'pagerduty'">
              <div>
                <label class="mb-1 block text-sm font-medium">Routing Key</label>
                <input
                  v-model="configs.pagerduty.routing_key"
                  type="password"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="Enter PagerDuty routing key"
                />
              </div>
            </template>

            <!-- Rollbar -->
            <template v-if="fwd.forwarder_type === 'rollbar'">
              <div>
                <label class="mb-1 block text-sm font-medium">Access Token</label>
                <input
                  v-model="configs.rollbar.access_token"
                  type="password"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="Enter Rollbar access token"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Environment</label>
                <input
                  v-model="configs.rollbar.environment"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="production"
                />
              </div>
            </template>

            <!-- OpsGenie -->
            <template v-if="fwd.forwarder_type === 'opsgenie'">
              <div>
                <label class="mb-1 block text-sm font-medium">API Key</label>
                <input
                  v-model="configs.opsgenie.api_key"
                  type="password"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="Enter OpsGenie API key"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Team</label>
                <input
                  v-model="configs.opsgenie.team"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="sre"
                />
              </div>
            </template>

            <!-- Loki -->
            <template v-if="fwd.forwarder_type === 'loki'">
              <div>
                <label class="mb-1 block text-sm font-medium">Push URL</label>
                <input
                  v-model="configs.loki.push_url"
                  type="url"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="https://loki.example.com/loki/api/v1/push"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Tenant ID</label>
                <input
                  v-model="configs.loki.tenant_id"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="my-tenant"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Labels</label>
                <input
                  v-model="configs.loki.labels"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="app=modulo, env=prod"
                />
                <p class="mt-1 text-xs text-muted-foreground">Comma-separated key=value pairs</p>
              </div>
            </template>

            <div class="flex items-center gap-3 pt-2">
              <button
                type="button"
                :disabled="testing[fwd.forwarder_type]"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
                @click="testConnection(fwd)"
              >
                {{ testing[fwd.forwarder_type] ? 'Testing...' : 'Test Connection' }}
              </button>
              <button
                type="button"
                :disabled="saving[fwd.forwarder_type]"
                class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                @click="saveConfig(fwd)"
              >
                {{ saving[fwd.forwarder_type] ? 'Saving...' : 'Save' }}
              </button>
            </div>

            <div
              v-if="testResults[fwd.forwarder_type]"
              class="rounded-lg border p-3 text-sm"
              :class="testResults[fwd.forwarder_type]?.ok ? 'border-success/50 bg-success/10 text-success' : 'border-destructive/50 bg-destructive/10 text-destructive'"
            >
              <p class="font-medium">{{ testResults[fwd.forwarder_type]?.ok ? 'Connection successful' : 'Connection failed' }}</p>
              <p class="mt-0.5">{{ testResults[fwd.forwarder_type]?.message }}</p>
            </div>

            <div
              v-if="formSuccess[fwd.forwarder_type]"
              class="rounded-lg border border-success/50 bg-success/10 p-3 text-sm text-success"
            >
              {{ formSuccess[fwd.forwarder_type] }}
            </div>
            <div
              v-if="formErrors[fwd.forwarder_type]"
              class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
            >
              {{ formErrors[fwd.forwarder_type] }}
            </div>
          </div>
        </div>
      </div>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { formatApiError, type ProblemDetail } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import { api } from '../lib/api/client'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

interface ForwarderItem {
  forwarder_type: string
  display_name: string
  enabled: boolean
  configured: boolean
  last_test_at: string | null
  last_test_ok: boolean | null
}

interface TestResult {
  ok: boolean
  message: string
}

interface ForwarderConfigs {
  sentry: Record<string, string>
  datadog: Record<string, string>
  pagerduty: Record<string, string>
  rollbar: Record<string, string>
  opsgenie: Record<string, string>
  loki: Record<string, string>
}

const planStore = usePlanStore()

const loading = ref(true)
const loadError = ref<string | null>(null)
const forwarders = ref<ForwarderItem[]>([])
const expanded = ref<Record<string, boolean>>({})
const testing = ref<Record<string, boolean>>({})
const saving = ref<Record<string, boolean>>({})
const testResults = ref<Record<string, TestResult | null>>({})
const formSuccess = ref<Record<string, string | null>>({})
const formErrors = ref<Record<string, string | null>>({})
const errorFwdTimeouts = ref<Record<string, ReturnType<typeof setTimeout>>>({})

const configs = reactive<ForwarderConfigs>({
  sentry: {},
  datadog: {},
  pagerduty: {},
  rollbar: {},
  opsgenie: {},
  loki: {},
})

async function loadForwarders() {
  loading.value = true
  loadError.value = null
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/errors/forwarders')
    if (err) {
      loadError.value = err && typeof err === 'object' && 'detail' in err
        ? `Failed to load forwarders: ${(err as ProblemDetail).detail}`
        : `Failed to load forwarders: ${formatApiError(err)}`
    } else if (data) {
      forwarders.value = data.forwarders
      for (const fwd of data.forwarders) {
        if (fwd.configured) {
          expanded.value[fwd.forwarder_type] = true
        }
      }
    }
  } catch (e: unknown) {
    loadError.value = `Failed to load forwarders: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

function toggleForwarder(fwd: ForwarderItem) {
  expanded.value[fwd.forwarder_type] = !expanded.value[fwd.forwarder_type]
}

function buildConfigJson(forwarderType: string): Record<string, string> {
  const cfg = configs[forwarderType as keyof ForwarderConfigs]
  const result: Record<string, string> = {}
  for (const [k, v] of Object.entries(cfg)) {
    if (v) result[k] = v
  }
  return result
}

async function saveConfig(fwd: ForwarderItem) {
  const ftype = fwd.forwarder_type
  saving.value[ftype] = true
  formErrors.value[ftype] = null
  formSuccess.value[ftype] = null
  try {
    const configJson = buildConfigJson(ftype)
    const { data, error: err } = await (api as any).PUT('/api/v1/errors/forwarders/{forwarder_type}', {
      params: { path: { forwarder_type: ftype } },
      body: {
        enabled: fwd.enabled,
        config_json: configJson,
      },
    })
    if (err) {
      formErrors.value[ftype] = err && typeof err === 'object' && 'detail' in err
        ? `Save failed: ${(err as ProblemDetail).detail}`
        : `Save failed: ${formatApiError(err)}`
    } else {
      formSuccess.value[ftype] = 'Configuration saved.'
      if (errorFwdTimeouts.value[ftype]) clearTimeout(errorFwdTimeouts.value[ftype])
      errorFwdTimeouts.value[ftype] = setTimeout(() => { formSuccess.value[ftype] = null }, 3000)
    }
  } catch (e: unknown) {
    formErrors.value[ftype] = `Save failed: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    saving.value[ftype] = false
  }
}

async function testConnection(fwd: ForwarderItem) {
  const ftype = fwd.forwarder_type
  testing.value[ftype] = true
  testResults.value[ftype] = null
  try {
    const configJson = buildConfigJson(ftype)
    const { data, error: err } = await (api as any).POST('/api/v1/errors/forwarders/{forwarder_type}/test', {
      params: { path: { forwarder_type: ftype } },
      body: { config_json: configJson },
    })
    if (err) {
      testResults.value[ftype] = { ok: false, message: String(err) }
    } else if (data) {
      testResults.value[ftype] = data
      if (errorFwdTimeouts.value[ftype]) clearTimeout(errorFwdTimeouts.value[ftype])
      errorFwdTimeouts.value[ftype] = setTimeout(() => { testResults.value[ftype] = null }, 10000)
    }
  } catch (e: unknown) {
    testResults.value[ftype] = { ok: false, message: e instanceof Error ? e.message : String(e) }
  } finally {
    testing.value[ftype] = false
  }
}

onBeforeUnmount(() => {
  for (const tid of Object.values(errorFwdTimeouts.value)) {
    if (tid) clearTimeout(tid)
  }
})

onMounted(() => {
  planStore.fetchPlan()
  loadForwarders()
})
</script>
