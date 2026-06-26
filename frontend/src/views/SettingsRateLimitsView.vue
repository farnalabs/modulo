<template>
  <div class="mx-auto max-w-4xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Rate Limits</h1>
      <p class="mt-1 text-muted-foreground">Configure per-route rate limiting thresholds</p>
    </header>

    <div v-if="loading" class="flex items-center justify-center py-16">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="loadError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
      {{ loadError }}
      <button class="ml-2 underline" @click="loadRules">Retry</button>
    </div>

    <div v-else class="space-y-6">
      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <div class="mb-4 flex items-center justify-between">
          <h2 class="text-lg font-semibold">Mode</h2>
          <span
            class="rounded-full px-3 py-1 text-xs font-medium"
            :class="mode === 'redis' ? 'bg-green-100 text-green-800' : 'bg-amber-100 text-amber-800'"
          >
            {{ mode === 'redis' ? 'Redis' : 'In-Memory' }}
          </span>
        </div>
        <p class="text-sm text-muted-foreground">
          {{ mode === 'redis' ? 'Rate limiting is backed by Redis.' : 'Rate limiting uses in-memory token buckets (Redis not configured).' }}
        </p>
      </div>

      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-lg font-semibold">Rules</h2>
        <table v-if="rules.length > 0" class="w-full text-sm">
          <thead>
            <tr class="border-b text-left text-muted-foreground">
              <th class="pb-2 font-medium">Path Prefix</th>
              <th class="pb-2 font-medium">Max Requests</th>
              <th class="pb-2 font-medium">Window (s)</th>
            </tr>
          </thead>
          <tbody>
             <tr v-for="rule in rules" :key="rule.path_prefix" class="border-b last:border-b-0">
              <td class="py-3 font-mono text-xs">{{ rule.path_prefix }}</td>
              <td class="py-3">
                <input
                  v-model.number="rule.max_requests"
                  type="number"
                  min="1"
                  class="w-24 rounded-lg border border-input bg-background px-3 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </td>
              <td class="py-3">
                <input
                  v-model.number="rule.window_s"
                  type="number"
                  min="1"
                  class="w-24 rounded-lg border border-input bg-background px-3 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="text-sm text-muted-foreground">No rate limit rules configured.</div>
      </div>

      <div v-if="formError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
        {{ formError }}
      </div>
      <div v-if="formSuccess" class="rounded-lg border border-green-500/50 bg-green-50 p-4 text-sm text-green-800">
        {{ formSuccess }}
      </div>

      <div class="flex items-center gap-3 pt-2">
        <div class="flex-1" />
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
          @click="resetForm"
        >
          Reset
        </button>
        <button
          type="button"
          :disabled="saving"
          class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          @click="saveRules"
        >
          {{ saving ? 'Saving...' : 'Save' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'

interface RateLimitRule {
  path_prefix: string
  max_requests: number
  window_s: number
}

interface RateLimitStatus {
  mode: string
  rules: RateLimitRule[]
}

const loading = ref(true)
const loadError = ref<string | null>(null)
const mode = ref('in_memory')
const rules = ref<RateLimitRule[]>([])
const saving = ref(false)
const formError = ref<string | null>(null)
const formSuccess = ref<string | null>(null)

const savedRules = ref<RateLimitRule[]>([])

async function loadRules() {
  loading.value = true
  loadError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/rate-limits')
    if (err) {
      loadError.value = `Failed to load rate limits: ${err}`
    } else if (data) {
      const status = data as unknown as RateLimitStatus
      mode.value = status.mode
      rules.value = JSON.parse(JSON.stringify(status.rules))
      savedRules.value = JSON.parse(JSON.stringify(status.rules))
    }
  } catch (e: unknown) {
    loadError.value = `Failed to load rate limits: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

function resetForm() {
  formError.value = null
  formSuccess.value = null
  rules.value = JSON.parse(JSON.stringify(savedRules.value))
}

async function saveRules() {
  saving.value = true
  formError.value = null
  formSuccess.value = null
  try {
    const { data, error: err } = await api.PUT('/api/v1/admin/rate-limits', {
      body: { rules: rules.value },
    })
    if (err) {
      formError.value = `Save failed: ${err}`
    } else if (data) {
      const status = data as unknown as RateLimitStatus
      mode.value = status.mode
      rules.value = JSON.parse(JSON.stringify(status.rules))
      savedRules.value = JSON.parse(JSON.stringify(status.rules))
      formSuccess.value = 'Rate limits updated successfully.'
      setTimeout(() => { formSuccess.value = null }, 3000)
    }
  } catch (e: unknown) {
    formError.value = `Save failed: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    saving.value = false
  }
}

onMounted(loadRules)
</script>
