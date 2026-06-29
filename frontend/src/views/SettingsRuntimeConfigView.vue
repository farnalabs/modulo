<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Runtime Configuration</h1>
      <p class="mt-1 text-muted-foreground">
        View and override configuration without restarting the server.
      </p>
    </header>

    <div class="flex items-center gap-3">
      <div v-if="hasDrift" class="flex items-center gap-2 rounded-lg border border-warning/50 bg-warning/10 px-4 py-2 text-sm text-warning">
        <span>⚠</span>
        <span>Some values differ from environment — restart to sync.</span>
      </div>
      <button
        type="button"
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
        data-testid="settings-runtime-config-reload"
        :disabled="loading"
        @click="reloadConfig"
      >
        Reload from env
      </button>
    </div>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadConfig" />

    <div v-else class="rounded-lg border">
      <table class="w-full">
        <thead>
          <tr class="border-b text-left text-sm font-medium text-muted-foreground">
            <th class="px-4 py-3">Key</th>
            <th class="px-4 py-3">Current Value</th>
            <th class="px-4 py-3">Expected (env)</th>
            <th class="px-4 py-3">Default</th>
            <th class="px-4 py-3">Provenance</th>
            <th class="px-4 py-3">Actions</th>
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
                  <span v-if="entryHasDrift(entry)" class="text-warning" title="Value differs from environment">⚠</span>
                <code class="text-sm font-mono">{{ entry.key }}</code>
                  <span
                    v-if="entry.hot_reloadable"
                    class="badge badge-status-success"
                  >
                    hot
                  </span>
                  <span
                    v-else
                    class="badge badge-status-muted"
                    title="Requires server restart"
                  >
                    static
                  </span>
              </div>
            </td>

            <td class="px-4 py-3">
              <input
                v-if="entry.hot_reloadable"
                v-model="editedValues[entry.key]"
                data-testid="settings-runtime-config-value"
                :class="inputClasses(entry)"
                @input="markEdited(entry.key)"
              />
              <code v-else class="text-sm font-mono break-all max-w-xs inline-block">
                {{ entry.current_value || '(empty)' }}
              </code>
            </td>

            <td class="px-4 py-3">
              <code class="text-sm font-mono text-muted-foreground break-all max-w-xs inline-block">
                {{ entry.env_value || '(not set)' }}
              </code>
            </td>

            <td class="px-4 py-3">
              <code class="text-sm text-muted-foreground break-all max-w-xs inline-block">
                {{ entry.default_value || '(none)' }}
              </code>
            </td>

            <td class="px-4 py-3">
              <span :class="provenanceBadgeClass(entry.provenance)">
                {{ entry.provenance }}
              </span>
            </td>

            <td class="px-4 py-3">
              <button
                v-if="isEdited(entry.key)"
                class="text-sm text-primary hover:underline"
                data-testid="settings-runtime-config-apply"
                :disabled="saving"
                @click="applyOverride(entry.key)"
              >
                Apply
              </button>
              <button
                v-if="entry.override_value"
                class="ml-2 text-sm text-destructive hover:underline"
                data-testid="settings-runtime-config-reset"
                :disabled="saving"
                @click="clearOverride(entry.key)"
              >
                Reset
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
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

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

const loading = ref(true)
const error = ref<string | null>(null)
const saving = ref(false)
const formError = ref<string | null>(null)
const formSuccess = ref<string | null>(null)
const items = ref<ConfigEntry[]>([])
const hasDrift = ref(false)
const editedValues = reactive<Record<string, string>>({})
const editedKeys = reactive(new Set<string>())

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

async function loadConfig() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/runtime-config')
    if (err) {
      error.value = `Failed to load runtime config: ${err}`
    } else if (data) {
      applyResponse(data as unknown as ConfigResponse)
    }
  } catch (e: unknown) {
    error.value = `Failed to load runtime config: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

async function reloadConfig() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/admin/runtime-config/reload')
    if (err) {
      error.value = `Failed to reload config: ${err}`
    } else if (data) {
      applyResponse(data as unknown as ConfigResponse)
    }
  } catch (e: unknown) {
    error.value = `Failed to reload config: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
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
      formError.value = `Failed to apply override: ${err}`
    } else if (data) {
      applyResponse(data as unknown as ConfigResponse)
      formSuccess.value = `Override applied for ${key}.`
      setTimeout(() => { formSuccess.value = null }, 3000)
    }
  } catch (e: unknown) {
    formError.value = `Failed to apply override: ${e instanceof Error ? e.message : String(e)}`
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
      formError.value = `Failed to clear override: ${err}`
    } else if (data) {
      applyResponse(data as unknown as ConfigResponse)
      formSuccess.value = `Override cleared for ${key}.`
      setTimeout(() => { formSuccess.value = null }, 3000)
    }
  } catch (e: unknown) {
    formError.value = `Failed to clear override: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    saving.value = false
  }
}

onMounted(loadConfig)
</script>
