<template>
  <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.SettingsErrorForwardersView.error_forwarders')" :subtitle="$t('views.SettingsErrorForwardersView.configure_external_error_tracking_and_alerting_integrations')" />

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
                    :title="fwd.last_test_ok === true ? $t('views.SettingsErrorForwardersView.last_test_passed') : fwd.last_test_ok === false ? $t('views.SettingsErrorForwardersView.last_test_failed') : $t('views.SettingsErrorForwardersView.not_tested')"
                  />
                  <span class="text-xs text-muted-foreground">
                    {{ fwd.last_test_ok === true ? $t('views.SettingsErrorForwardersView.connected') : fwd.last_test_ok === false ? $t('views.SettingsErrorForwardersView.failed') : $t('views.SettingsErrorForwardersView.not_tested') }}
                  </span>
                </div>
              </div>
            </div>
            <div class="flex items-center gap-3">
              <span
                v-if="!fwd.configured"
                class="rounded-full bg-amber-500/10 px-2.5 py-0.5 text-xs font-medium text-amber-600"
              >{{ $t('views.SettingsErrorForwardersView.not_configured') }}</span>
              <button
                type="button"
                class="relative inline-flex h-6 w-11 cursor-pointer items-center"
                :aria-label="$t('views.SettingsErrorForwardersView.toggle_forwarder', { name: fwd.display_name })"
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
                <label for="settingserrorforwardersview-field-13" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.dsn') }}</label>
                <input id="settingserrorforwardersview-field-13"
                  v-model="configs.sentry.dsn"
                  type="text"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.dsn_placeholder')"
                />
              </div>
              <div>
                <label for="settingserrorforwardersview-field-12" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.org_slug') }}</label>
                <input id="settingserrorforwardersview-field-12"
                  v-model="configs.sentry.org_slug"
                  type="text"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.org_slug_placeholder')"
                />
              </div>
              <div>
                <label for="settingserrorforwardersview-field-11" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.project_slug') }}</label>
                <input id="settingserrorforwardersview-field-11"
                  v-model="configs.sentry.project_slug"
                  type="text"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.project_slug_placeholder')"
                />
              </div>
            </template>

            <!-- DataDog -->
            <template v-if="fwd.forwarder_type === 'datadog'">
              <div>
                <label for="settingserrorforwardersview-field-10" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.api_key') }}</label>
                <input id="settingserrorforwardersview-field-10"
                  v-model="configs.datadog.api_key"
                  type="password"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.datadog_api_key_placeholder')"
                />
              </div>
              <div>
                <label for="settingserrorforwardersview-datadog-site" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.site') }}</label>
                <Select
  v-model="configs.datadog.site"
  :aria-label="$t('views.SettingsErrorForwardersView.site')"
  :placeholder="$t('views.SettingsErrorForwardersView.select_site')"
  id="settingserrorforwardersview-datadog-site"
  class="input-base"
  :options="[{ value: 'datadoghq.com', label: 'US (datadoghq.com)' }, { value: 'datadoghq.eu', label: 'EU (datadoghq.eu)' }, { value: 'us3.datadoghq.com', label: 'US3 (us3.datadoghq.com)' }, { value: 'us5.datadoghq.com', label: 'US5 (us5.datadoghq.com)' }, { value: 'ddog-gov.com', label: $t('views.SettingsErrorForwardersView.gov_ddog_gov_com') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              </div>
            </template>

            <!-- PagerDuty -->
            <template v-if="fwd.forwarder_type === 'pagerduty'">
              <div>
                <label for="settingserrorforwardersview-field-8" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.routing_key') }}</label>
                <input id="settingserrorforwardersview-field-8"
                  v-model="configs.pagerduty.routing_key"
                  type="password"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.pagerduty_routing_key_placeholder')"
                />
              </div>
            </template>

            <!-- Rollbar -->
            <template v-if="fwd.forwarder_type === 'rollbar'">
              <div>
                <label for="settingserrorforwardersview-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.access_token') }}</label>
                <input id="settingserrorforwardersview-field-7"
                  v-model="configs.rollbar.access_token"
                  type="password"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.rollbar_access_token_placeholder')"
                />
              </div>
              <div>
                <label for="settingserrorforwardersview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.environment') }}</label>
                <input id="settingserrorforwardersview-field-6"
                  v-model="configs.rollbar.environment"
                  type="text"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.environment_placeholder')"
                />
              </div>
            </template>

            <!-- OpsGenie -->
            <template v-if="fwd.forwarder_type === 'opsgenie'">
              <div>
                <label for="settingserrorforwardersview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.api_key') }}</label>
                <input id="settingserrorforwardersview-field-5"
                  v-model="configs.opsgenie.api_key"
                  type="password"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.opsgenie_api_key_placeholder')"
                />
              </div>
              <div>
                <label for="settingserrorforwardersview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.team') }}</label>
                <input id="settingserrorforwardersview-field-4"
                  v-model="configs.opsgenie.team"
                  type="text"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.team_placeholder')"
                />
              </div>
            </template>

            <!-- Loki -->
            <template v-if="fwd.forwarder_type === 'loki'">
              <div>
                <label for="settingserrorforwardersview-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.push_url') }}</label>
                <input id="settingserrorforwardersview-field-3"
                  v-model="configs.loki.push_url"
                  type="url"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.push_url_placeholder')"
                />
              </div>
              <div>
                <label for="settingserrorforwardersview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.tenant_id') }}</label>
                <input id="settingserrorforwardersview-field-2"
                  v-model="configs.loki.tenant_id"
                  type="text"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.tenant_id_placeholder')"
                />
              </div>
              <div>
                <label for="settingserrorforwardersview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsErrorForwardersView.labels') }}</label>
                <input id="settingserrorforwardersview-field-1"
                  v-model="configs.loki.labels"
                  type="text"
                  class="input-base"
                  :placeholder="$t('views.SettingsErrorForwardersView.labels_placeholder')"
                />
                <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.SettingsErrorForwardersView.labels_hint') }}</p>
              </div>
            </template>

            <div class="flex items-center gap-3 pt-2">
              <button
                type="button"
                :disabled="testing[fwd.forwarder_type]"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
                @click="testConnection(fwd)"
              >
                {{ testing[fwd.forwarder_type] ? $t('views.SettingsErrorForwardersView.testing') : $t('views.SettingsErrorForwardersView.test_connection') }}
              </button>
              <Button type="button" :disabled="saving[fwd.forwarder_type]" @click="saveConfig(fwd)">
                {{ saving[fwd.forwarder_type] ? $t('views.SettingsErrorForwardersView.saving') : $t('views.SettingsErrorForwardersView.save') }}
              </Button>
            </div>

            <div
              v-if="testResults[fwd.forwarder_type]"
              class="rounded-lg border p-3 text-sm"
              :class="testResults[fwd.forwarder_type]?.ok ? 'border-success/50 bg-success/10 text-success' : 'border-destructive/50 bg-destructive/10 text-destructive'"
            >
              <p class="font-medium">{{ testResults[fwd.forwarder_type]?.ok ? $t('views.SettingsErrorForwardersView.connection_successful') : $t('views.SettingsErrorForwardersView.connection_failed') }}</p>
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
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import { api } from '../lib/api/client'
import FeatureGate from '../components/FeatureGate.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Button from 'primevue/button'
import Select from 'primevue/select'

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
const { t } = useI18n()

const { loading, error: loadError, data: forwarders, load: loadForwarders } = useDataFetch<ForwarderItem[]>(
  async () => {
    const { data, error: err } = await api.GET('/api/v1/errors/forwarders')
    if (err) return { error: err }
    const items = (data?.forwarders ?? []) as ForwarderItem[]
    for (const fwd of items) {
      if (fwd.configured) {
        expanded.value[fwd.forwarder_type] = true
      }
    }
    return { data: items }
  },
  { initialValue: [] as ForwarderItem[] }
)
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

function toggleForwarder(fwd: ForwarderItem) {
  fwd.enabled = !fwd.enabled
  if (fwd.enabled) {
    expanded.value[fwd.forwarder_type] = true
  }
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
    const { error: err } = await api.PUT('/api/v1/errors/forwarders/{forwarder_type}', {
      params: { path: { forwarder_type: ftype } },
      body: {
        enabled: fwd.enabled,
        config_json: configJson,
      },
    })
    if (err) {
      formErrors.value[ftype] = t('views.SettingsErrorForwardersView.save_failed', { detail: formatApiError(err) })
    } else {
      formSuccess.value[ftype] = t('views.SettingsErrorForwardersView.configuration_saved')
      if (errorFwdTimeouts.value[ftype]) clearTimeout(errorFwdTimeouts.value[ftype])
      errorFwdTimeouts.value[ftype] = setTimeout(() => { formSuccess.value[ftype] = null }, 3000)
    }
  } catch (e: unknown) {
    formErrors.value[ftype] = t('views.SettingsErrorForwardersView.save_failed', { detail: formatApiError(e) })
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
    const { data, error: err } = await api.POST('/api/v1/errors/forwarders/{forwarder_type}/test', {
      params: { path: { forwarder_type: ftype } },
      body: { config_json: configJson },
    })
    if (err) {
      testResults.value[ftype] = { ok: false, message: formatApiError(err) }
    } else if (data) {
      testResults.value[ftype] = data
      if (errorFwdTimeouts.value[ftype]) clearTimeout(errorFwdTimeouts.value[ftype])
      errorFwdTimeouts.value[ftype] = setTimeout(() => { testResults.value[ftype] = null }, 10000)
    }
  } catch (e: unknown) {
    testResults.value[ftype] = { ok: false, message: formatApiError(e) }
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
})
</script>
