<template>
  <div class="mx-auto max-w-6xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Dashboard</h1>
      <p class="mt-1 text-muted-foreground">Overview of your organisation's pipelines and runs</p>
    </header>

    <div v-if="loading" class="flex items-center justify-center py-16">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
      {{ error }}
    </div>

    <template v-else-if="data">
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Total Runs</p>
          <p class="mt-1 text-3xl font-bold">{{ data.total_runs }}</p>
        </div>
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Active Pipelines</p>
          <p class="mt-1 text-3xl font-bold">{{ data.active_pipelines }}</p>
        </div>
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Running</p>
          <p class="mt-1 text-3xl font-bold text-success">{{ data.run_counts_by_status.running }}</p>
        </div>
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Awaiting Human</p>
          <p class="mt-1 text-3xl font-bold text-warning">{{ data.run_counts_by_status.awaiting_human }}</p>
        </div>
      </div>

      <div class="grid gap-4 sm:grid-cols-2">
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Failed</p>
          <p class="mt-1 text-3xl font-bold text-destructive">{{ data.run_counts_by_status.failed }}</p>
        </div>
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Idle</p>
          <p class="mt-1 text-3xl font-bold text-muted-foreground">{{ data.run_counts_by_status.idle }}</p>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'

interface DashboardSummary {
  total_runs: number
  active_pipelines: number
  run_counts_by_status: {
    running: number
    awaiting_human: number
    failed: number
    idle: number
  }
}

const data = ref<DashboardSummary | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

onMounted(async () => {
  try {
    const { data: result, error: err } = await api.GET('/dashboard/summary')
    if (err) {
      error.value = `Failed to load dashboard: ${err}`
    } else {
      data.value = result as unknown as DashboardSummary
    }
  } catch (e: unknown) {
    error.value = `Failed to load dashboard: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
})
</script>
