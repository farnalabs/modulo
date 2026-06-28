<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Runtime Configuration</h1>
      <p class="mt-1 text-muted-foreground">
        View and override configuration without restarting the server.
      </p>
    </header>

    <div class="flex items-center gap-3">
      <div v-if="hasDrift" class="flex items-center gap-2 rounded-lg border border-amber-500/50 bg-amber-50 px-4 py-2 text-sm text-amber-800">
        <span>⚠</span>
        <span>Some values differ from environment — restart to sync.</span>
      </div>
      <button
        type="button"
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
        :disabled="loading"
        @click="reloadConfig"
      >
        Reload from env
      </button>
    </div>

    <div v-if="loading" class="flex items-center justify-center py-16">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
      {{ error }}
      <button class="ml-2 underline" @click="loadConfig">Retry</button>
    </div>

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
            :class="{ 'bg-amber-50/50': entryHasDrift(entry) }"
          >
            <td class="px-4 py-3">
              <div class="flex items-center gap-2">
                <span v-if="entryHasDrift(entry)" class="text-amber-500" title="Value differs from environment">⚠</span>
                <code class="text-sm font-mono">{{ entry.key }}</code>
                <span
                  v-if="entry.hot_reloadable"
                  class="rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-700"
                >
                  hot
                </span>
                <span
                  v-else
                  class="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500"
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
                :disabled="saving"
                @click="applyOverride(entry.key)"
              >
                Apply
              </button>
              <button
                v-if="entry.override_value"
                class="ml-2 text-sm text-destructive hover:underline"
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
    <div v-if="formSuccess" class="rounded-lg border border-green-500/50 bg-green-50 p-4 text-sm text-green-800">
      {{ formSuccess }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../lib/api/client'

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
  const borderColor = isEdited(entry.key) ? 'border-amber-400' : 'border-input'
  return `${base} ${borderColor}`
}

function provenanceBadgeClass(provenance: string): string {
  switch (provenance) {
    case 'override': return 'inline-block rounded-full px-2.5 py-0.5 text-xs font-medium bg-blue-100 text-blue-700'
    case 'environment': return 'inline-block rounded-full px-2.5 py-0.5 text-xs font-medium bg-purple-100 text-purple-700'
    case 'default': return 'inline-block rounded-full px-2.5 py-0.5 text-xs font-medium bg-gray-100 text-gray-500'
    default: return 'inline-block rounded-full px-2.5 py-0.5 text-xs font-medium bg-gray-100 text-gray-700'
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
