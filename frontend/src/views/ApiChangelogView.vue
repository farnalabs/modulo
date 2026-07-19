<template>
  <div data-theme="agent" class="page-narrow">
    <PageHeader :title="$t('views.ApiChangelogView.api_changelog')" data-test-id="changelog-title" :subtitle="$t('views.ApiChangelogView.version_history_and_deprecation_notices_for_the_modulo_api')" />

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadChangelog" />

    <div v-else-if="entries.length === 0" data-testid="changelog-empty" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">{{ $t('views.ApiChangelogView.no_changelog_entries') }}</p>
      <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.ApiChangelogView.no_api_version_history_is_available_yet') }}</p>
    </div>

    <template v-else>
      <div data-testid="changelog-list"
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
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import PageHeader from '../components/shared/PageHeader.vue'
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

const { loading, error, data: entries, load: loadChangelog } = useDataFetch<ChangelogEntry[]>(
  async () => {
    const response = await api.GET('/api/v1/changelog')
    return { data: response.data as unknown as ChangelogEntry[] | undefined, error: response.error }
  },
  { initialValue: [] as ChangelogEntry[] },
)
</script>
