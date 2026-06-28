<template>
  <div class="mx-auto max-w-6xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Dashboard</h1>
      <p class="mt-1 text-muted-foreground">Overview of your organisation's pipelines and runs</p>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" />

    <template v-else-if="data">
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div class="card card-hover p-4">
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/></svg>
            </div>
            <div class="min-w-0">
              <p class="text-sm font-medium text-muted-foreground">Total Runs</p>
              <p class="text-2xl font-bold stat-card-number">{{ data.total_runs }}</p>
            </div>
          </div>
        </div>
        <div class="card card-hover p-4">
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
            </div>
            <div class="min-w-0">
              <p class="text-sm font-medium text-muted-foreground">Active Pipelines</p>
              <p class="text-2xl font-bold stat-card-number">{{ data.active_pipelines }}</p>
            </div>
          </div>
        </div>
        <div class="card card-hover p-4">
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-success/10 text-success">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
            </div>
            <div class="min-w-0">
              <p class="text-sm font-medium text-muted-foreground">Running</p>
              <p class="text-2xl font-bold text-success">{{ data.run_counts_by_status.running }}</p>
            </div>
          </div>
        </div>
        <div class="card card-hover p-4">
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-warning/10 text-warning">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            </div>
            <div class="min-w-0">
              <p class="text-sm font-medium text-muted-foreground">Awaiting Human</p>
              <p class="text-2xl font-bold text-warning">{{ data.run_counts_by_status.awaiting_human }}</p>
            </div>
          </div>
        </div>
      </div>

      <div class="grid gap-4 sm:grid-cols-2">
        <div class="card card-hover p-4">
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-destructive/10 text-destructive">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
            </div>
            <div class="min-w-0">
              <p class="text-sm font-medium text-muted-foreground">Failed</p>
              <p class="text-2xl font-bold text-destructive">{{ data.run_counts_by_status.failed }}</p>
            </div>
          </div>
        </div>
        <div class="card card-hover p-4">
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-muted text-muted-foreground">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            </div>
            <div class="min-w-0">
              <p class="text-sm font-medium text-muted-foreground">Idle</p>
              <p class="text-2xl font-bold">{{ data.run_counts_by_status.idle }}</p>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

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
    const { data: result, error: err } = await api.GET('/api/v1/dashboard/summary')
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
