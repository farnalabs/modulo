<template>
  <div class="mx-auto max-w-4xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">API Changelog</h1>
      <p class="mt-1 text-muted-foreground">Version history and deprecation notices for the Modulo API</p>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadChangelog" />

    <div v-else-if="entries.length === 0" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">No changelog entries</p>
      <p class="mt-1 text-sm text-muted-foreground">No API version history is available yet.</p>
    </div>

    <template v-else>
      <div
        v-for="entry in entries"
        :key="entry.version"
        class="rounded-lg border bg-card shadow-sm"
      >
        <div class="border-b bg-muted/30 px-6 py-4">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-3">
              <span class="badge badge-context-blue text-sm font-semibold">
                v{{ entry.version }}
              </span>
              <time class="text-sm text-muted-foreground">{{ entry.date }}</time>
            </div>
            <a
              v-if="entry.migration_url"
              :href="entry.migration_url"
              class="text-sm font-medium text-primary hover:underline"
            >
              Migration guide &rarr;
            </a>
          </div>
          <p class="mt-2 text-base font-medium">{{ entry.summary }}</p>
        </div>

        <div class="px-6 py-4">
          <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wide">Changes</h3>
          <ul class="space-y-1.5">
            <li
              v-for="(change, i) in entry.changes"
              :key="i"
              class="flex items-start gap-2 text-sm"
            >
              <span class="mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
              <span>{{ change }}</span>
            </li>
          </ul>

          <div v-if="entry.deprecations && entry.deprecations.length > 0" class="mt-4">
            <h3 class="mb-2 text-sm font-semibold text-warning uppercase tracking-wide">Deprecations</h3>
            <ul class="space-y-1.5">
              <li
                v-for="(dep, i) in entry.deprecations"
                :key="i"
                class="flex items-start gap-2 text-sm text-warning"
              >
                <span class="mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full bg-warning" />
                <span>{{ dep }}</span>
              </li>
            </ul>
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

interface ChangelogEntry {
  version: string
  date: string
  summary: string
  changes: string[]
  deprecations: string[] | null
  migration_url: string | null
}

const entries = ref<ChangelogEntry[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

async function loadChangelog() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/changelog')
    if (err) {
      error.value = `Failed to load changelog: ${err}`
    } else if (data) {
      entries.value = data as unknown as ChangelogEntry[]
    }
  } catch (e: unknown) {
    error.value = `Failed to load changelog: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

onMounted(() => loadChangelog())
</script>
