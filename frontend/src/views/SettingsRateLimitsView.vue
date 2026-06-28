<template>
  <div class="mx-auto max-w-4xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Rate Limits</h1>
      <p class="mt-1 text-muted-foreground">View per-route rate limiting rules and current usage</p>
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
              <td class="py-3">{{ rule.max_requests }}</td>
              <td class="py-3">{{ rule.window_s }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="text-sm text-muted-foreground">No rate limit rules configured.</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'

type RateLimitRule = components['schemas']['RateLimitRuleResponse']

const loading = ref(true)
const loadError = ref<string | null>(null)
const mode = ref('in_memory')
const rules = ref<RateLimitRule[]>([])

async function loadRules() {
  loading.value = true
  loadError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/rate-limits')
    if (err) {
      loadError.value = `Failed to load rate limits: ${err}`
    } else if (data) {
      mode.value = data.mode
      rules.value = data.rules
    }
  } catch (e: unknown) {
    loadError.value = `Failed to load rate limits: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

onMounted(loadRules)
</script>
