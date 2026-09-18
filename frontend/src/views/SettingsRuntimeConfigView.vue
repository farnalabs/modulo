<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.SettingsRuntimeConfigView.runtime_configuration')" :subtitle="$t('views.SettingsRuntimeConfigView.description')" />

    <FeatureGate feature-name="runtime_config" required-tier="team" show-disabled>

    <div class="flex items-center gap-3">
      <div v-if="hasDrift" class="flex items-center gap-2 rounded-lg border border-warning/50 bg-warning/10 px-4 py-2 text-sm text-warning">
        <span>⚠</span>
        <span>{{ $t('views.SettingsRuntimeConfigView.some_values_differ_from_environment_restart_to_sync') }}</span>
      </div>
      <button
        type="button"
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
        data-testid="settings-runtime-config-reload"
        :disabled="loading"
        @click="reloadConfig"
      >
        {{ $t('views.SettingsRuntimeConfigView.reload_from_env') }}
      </button>
    </div>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error && !featureRequired" :message="error" :on-retry="loadConfig" />

    <div v-else-if="!featureRequired" class="rounded-lg border overflow-x-auto">
      <table class="w-full">
        <thead>
          <tr class="border-b text-left text-sm font-medium text-muted-foreground">
            <th class="px-4 py-3">{{ $t('views.SettingsRuntimeConfigView.key') }}</th>
            <th class="px-4 py-3">{{ $t('views.SettingsRuntimeConfigView.current_value') }}</th>
            <th class="px-4 py-3">{{ $t('views.SettingsRuntimeConfigView.expected_env') }}</th>
            <th class="px-4 py-3">{{ $t('views.SettingsRuntimeConfigView.default') }}</th>
            <th class="px-4 py-3">{{ $t('views.SettingsRuntimeConfigView.provenance') }}</th>
            <th class="px-4 py-3">{{ $t('views.SettingsRuntimeConfigView.actions') }}</th>
          </tr>
        </thead>
        <tbody>
            <tr
              v-for="entry in items"
              :key="entry.key"
              class="border-b last:border-0 hover:bg-muted/50 transition-colors"
              :class="{ 'bg-warning/5': entryHasDrift(entry) }"
            >
              <td class="px-4 py-3">
                <div class="flex items-center gap-2">
                  <span v-if="entryHasDrift(entry)" class="text-warning" :title="$t('views.SettingsRuntimeConfigView.value_differs_from_environment')">⚠</span>
                <code class="text-sm font-mono">{{ entry.key }}</code>
                  <span
                    v-if="entry.hot_reloadable"
                    class="badge badge-status-success"
                  >
                    {{ $t('views.SettingsRuntimeConfigView.hot') }}
                  </span>
                  <span
                    v-else
                    class="badge badge-status-muted"
                    :title="$t('views.SettingsRuntimeConfigView.requires_server_restart')"
                  >
                    {{ $t('views.SettingsRuntimeConfigView.static') }}
                  </span>
              </div>
            </td>

            <td class="px-4 py-3">
              <template v-if="isKeySensitive(entry.key) && !revealedKeys.has(entry.key)">
                <div class="flex items-center gap-2">
                  <code class="text-sm font-mono">********</code>
                  <button
                    type="button"
                    class="text-xs text-primary hover:underline"
                    @click="revealKey(entry.key)"
                  >
                    {{ $t('views.SettingsRuntimeConfigView.reveal') }}
                  </button>
                </div>
              </template>
              <template v-else>
                <input
                  v-if="entry.hot_reloadable"
                  v-model="editedValues[entry.key]"
                  data-testid="settings-runtime-config-value"
                  :aria-label="`${$t('views.SettingsRuntimeConfigView.current_value')}: ${entry.key}`"
                  :class="inputClasses(entry)"
                  @input="markEdited(entry.key)"
                />
                <code v-else class="text-sm font-mono break-all max-w-xs inline-block">
                  {{ entry.current_value || $t('views.SettingsRuntimeConfigView.empty_value') }}
                </code>
              </template>
            </td>

            <td class="px-4 py-3">
              <template v-if="isKeySensitive(entry.key) && !revealedKeys.has(entry.key)">
                <div class="flex items-center gap-2">
                  <code class="text-sm font-mono text-muted-foreground">********</code>
                  <button
                    type="button"
                    class="text-xs text-primary hover:underline"
                    @click="revealKey(entry.key)"
                  >
                    {{ $t('views.SettingsRuntimeConfigView.reveal') }}
                  </button>
                </div>
              </template>
              <code v-else class="text-sm font-mono text-muted-foreground break-all max-w-xs inline-block">
                {{ entry.env_value || $t('views.SettingsRuntimeConfigView.not_set') }}
              </code>
            </td>

            <td class="px-4 py-3">
              <template v-if="isKeySensitive(entry.key) && !revealedKeys.has(entry.key)">
                <div class="flex items-center gap-2">
                  <code class="text-sm text-muted-foreground">********</code>
                  <button
                    type="button"
                    class="text-xs text-primary hover:underline"
                    @click="revealKey(entry.key)"
                  >
                    {{ $t('views.SettingsRuntimeConfigView.reveal') }}
                  </button>
                </div>
              </template>
              <code v-else class="text-sm text-muted-foreground break-all max-w-xs inline-block">
                {{ entry.default_value || $t('views.SettingsRuntimeConfigView.none_value') }}
              </code>
            </td>

            <td class="px-4 py-3">
              <span :class="provenanceBadgeClass(entry.provenance)">
                {{ entry.provenance }}
              </span>
            </td>

            <td class="px-4 py-3">
              <button
                type="button"
                v-if="isEdited(entry.key)"
                class="text-sm text-primary hover:underline"
                data-testid="settings-runtime-config-apply"
                :disabled="saving"
                @click="applyOverride(entry.key)"
              >
                {{ $t('views.SettingsRuntimeConfigView.apply') }}
              </button>
              <button
                type="button"
                v-if="entry.override_value"
                class="ml-2 text-sm text-destructive hover:underline"
                data-testid="settings-runtime-config-reset"
                :disabled="saving"
                @click="clearOverride(entry.key)"
              >
                {{ $t('views.SettingsRuntimeConfigView.reset') }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="formError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
      {{ formError }}
    </div>
    <div v-if="formSuccess" class="rounded-lg border border-success/50 bg-success/10 p-4 text-sm text-success">
      {{ formSuccess }}
    </div>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, watch, onBeforeUnmount } from 'vue'
import { useDataFetch } from '../composables/useDataFetch'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'

const { t } = useI18n()

function isFeatureRequiredError(err: unknown): boolean {
  if (typeof err !== 'object' || err === null) return false
  const obj = err as Record<string, unknown>
  return obj.status === 402 || obj.type === 'urn:problem:modulo:feature_required'
}

interface ConfigEntry {
  key: string
  current_value: string | null
  default_value: string | null
  env_value: string | null
  override_value: string | null
  provenance: string
  hot_reloadable: boolean
}

interface ConfigResponse {
  items: ConfigEntry[]
  has_drift: boolean
}

const saving = ref(false)
const formError = ref<string | null>(null)
const formSuccess = ref<string | null>(null)
let runtimeConfigTimeout: ReturnType<typeof setTimeout> | null = null
const hasDrift = ref(false)
const editedValues = reactive<Record<string, string>>({})
const editedKeys = reactive(new Set<string>())

const items = ref<ConfigEntry[]>([])
const featureRequired = ref(false)
const { loading, error, data: configData, load: loadConfig } = useDataFetch<ConfigResponse>(
  async () => {
    const res = await api.GET('/api/v1/admin/runtime-config')
    if (res.error) {
      if (isFeatureRequiredError(res.error)) {
        featureRequired.value = true
        return { data: { items: [], has_drift: false } as ConfigResponse, error: undefined }
      }
      featureRequired.value = false
      return { data: undefined, error: { detail: formatApiError(res.error) } }
    }
    featureRequired.value = false
    return { data: res.data as unknown as ConfigResponse, error: undefined }
  },
)

watch(configData, (resp) => {
  if (resp) applyResponse(resp)
})

const SENSITIVE_KEY_PATTERNS = /SECRET|PASSWORD|TOKEN|KEY|DATABASE_URL|ENCRYPTION|SIGNING|PRIVATE/i

const revealedKeys = reactive(new Set<string>())

function isKeySensitive(key: string): boolean {
  return SENSITIVE_KEY_PATTERNS.test(key)
}

function revealKey(key: string): void {
  revealedKeys.add(key)
}

function entryHasDrift(entry: ConfigEntry): boolean {
  if (entry.override_value) return false
  return entry.env_value !== null && entry.current_value !== entry.env_value
}

function markEdited(key: string): void {
  editedKeys.add(key)
}

function isEdited(key: string): boolean {
  return editedKeys.has(key)
}

function inputClasses(entry: ConfigEntry): string {
  const base = 'w-full rounded-md border bg-background px-3 py-1.5 text-sm font-mono ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'
  const borderColor = isEdited(entry.key) ? 'border-warning' : 'border-input'
  return `${base} ${borderColor}`
}

function provenanceBadgeClass(provenance: string): string {
  switch (provenance) {
    case 'override': return 'badge badge-context-blue'
    case 'environment': return 'badge badge-context-purple'
    case 'default': return 'badge badge-context-slate'
    default: return 'badge badge-context-slate'
  }
}

function applyResponse(resp: ConfigResponse): void {
  items.value = resp.items
  hasDrift.value = resp.has_drift
  editedKeys.clear()
  for (const entry of resp.items) {
    editedValues[entry.key] = entry.current_value ?? ''
  }
}

async function reloadConfig() {
  formError.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/admin/runtime-config/reload')
    if (err) {
      formError.value = `${t('views.SettingsRuntimeConfigView.failed_to_reload_config')} ${formatApiError(err)}`
    } else if (data) {
      applyResponse(data as unknown as ConfigResponse)
    }
  } catch (e: unknown) {
    formError.value = `${t('views.SettingsRuntimeConfigView.failed_to_reload_config')} ${formatApiError(e)}`
  }
}

async function applyOverride(key: string) {
  saving.value = true
  formError.value = null
  formSuccess.value = null
  try {
    const { data, error: err } = await api.PUT('/api/v1/admin/runtime-config', {
      body: { overrides: { [key]: editedValues[key] } },
    })
    if (err) {
      formError.value = `${t('views.SettingsRuntimeConfigView.failed_to_apply_override')} ${formatApiError(err)}`
    } else if (data) {
      applyResponse(data as unknown as ConfigResponse)
      formSuccess.value = t('views.SettingsRuntimeConfigView.override_applied_for', { key })
      if (runtimeConfigTimeout) clearTimeout(runtimeConfigTimeout)
      runtimeConfigTimeout = setTimeout(() => { formSuccess.value = null }, 3000)
    }
  } catch (e: unknown) {
    formError.value = `${t('views.SettingsRuntimeConfigView.failed_to_apply_override')} ${formatApiError(e)}`
  } finally {
    saving.value = false
  }
}

async function clearOverride(key: string) {
  saving.value = true
  formError.value = null
  formSuccess.value = null
  try {
    const { data, error: err } = await api.PUT('/api/v1/admin/runtime-config', {
      body: { clear: [key] },
    })
    if (err) {
      formError.value = `${t('views.SettingsRuntimeConfigView.failed_to_clear_override')} ${formatApiError(err)}`
    } else if (data) {
      applyResponse(data as unknown as ConfigResponse)
      formSuccess.value = t('views.SettingsRuntimeConfigView.override_cleared_for', { key })
      if (runtimeConfigTimeout) clearTimeout(runtimeConfigTimeout)
      runtimeConfigTimeout = setTimeout(() => { formSuccess.value = null }, 3000)
    }
  } catch (e: unknown) {
    formError.value = `${t('views.SettingsRuntimeConfigView.failed_to_clear_override')} ${formatApiError(e)}`
  } finally {
    saving.value = false
  }
}

onBeforeUnmount(() => {
  if (runtimeConfigTimeout) clearTimeout(runtimeConfigTimeout)
})
</script>
