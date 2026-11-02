<template>
  <div class="page-wide space-y-6">
    <PageHeader
      :title="$t('views.EvidenceView.title')"
      :subtitle="$t('views.EvidenceView.subtitle')"
    />

    <section class="space-y-3" aria-labelledby="evidence-run-picker-heading" data-testid="evidence-run-picker">
      <div>
        <h2 id="evidence-run-picker-heading" class="text-base font-semibold">{{ $t('views.EvidenceView.run_picker_title') }}</h2>
        <p class="text-sm text-muted-foreground">{{ $t('views.EvidenceView.run_picker_description') }}</p>
      </div>
      <div class="flex flex-wrap items-end gap-3">
        <div class="min-w-72 flex-1">
          <label for="evidence-run-search" class="mb-1 block text-sm font-medium">
            {{ $t('views.EvidenceView.run_search') }}
          </label>
          <input
            id="evidence-run-search"
            v-model="runSearch"
            type="search"
            data-testid="evidence-run-search"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('views.EvidenceView.run_search_placeholder')"
          />
        </div>
        <div class="min-w-72 flex-1">
          <label for="evidence-recent-run" class="mb-1 block text-sm font-medium">
            {{ $t('views.EvidenceView.recent_run') }}
          </label>
          <select
            id="evidence-recent-run"
            v-model="runId"
            data-testid="evidence-recent-run"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :disabled="recentRunsLoading || recentRuns.length === 0"
          >
            <option value="">{{ recentRunsLoading ? $t('views.EvidenceView.recent_runs_loading') : $t('views.EvidenceView.choose_recent_run') }}</option>
            <option v-for="run in filteredRecentRuns" :key="run.run_id" :value="run.run_id">
              {{ runLabel(run) }}
            </option>
          </select>
        </div>
      </div>
      <div v-if="recentRunsError" class="text-sm text-destructive-text" aria-live="assertive" data-testid="evidence-recent-runs-error">
        {{ recentRunsError }}
      </div>
      <p v-else-if="!recentRunsLoading && recentRuns.length === 0" class="text-sm text-muted-foreground" data-testid="evidence-recent-runs-empty">
        {{ $t('views.EvidenceView.no_recent_runs') }}
      </p>
    </section>

    <form class="flex flex-wrap items-end gap-3" @submit.prevent="load">
      <div>
        <label for="evidence-run-id" class="mb-1 block text-sm font-medium">
          {{ $t('views.EvidenceView.manual_run_id') }}
        </label>
        <input
          id="evidence-run-id"
          v-model="runId"
          type="text"
          data-testid="evidence-run-id"
          class="w-72 rounded-lg border border-input bg-background px-3 py-2 text-sm"
          :placeholder="$t('views.EvidenceView.run_id_placeholder')"
        />
      </div>
      <div>
        <label for="evidence-node-id" class="mb-1 block text-sm font-medium">
          {{ $t('views.EvidenceView.node_id') }}
        </label>
        <input
          id="evidence-node-id"
          v-model="nodeId"
          type="text"
          data-testid="evidence-node-id"
          class="w-56 rounded-lg border border-input bg-background px-3 py-2 text-sm"
        />
      </div>
      <div>
        <label for="evidence-key" class="mb-1 block text-sm font-medium">
          {{ $t('views.EvidenceView.key') }}
        </label>
        <input
          id="evidence-key"
          v-model="keyFilter"
          type="text"
          data-testid="evidence-key"
          class="w-56 rounded-lg border border-input bg-background px-3 py-2 text-sm"
        />
      </div>
      <button
        type="submit"
        data-testid="evidence-load"
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-muted"
      >
        {{ $t('views.EvidenceView.load') }}
      </button>
    </form>

    <div
      v-if="loading"
      aria-live="polite"
      data-testid="evidence-loading"
      class="flex items-center gap-3 text-sm text-muted-foreground"
    >
      <LoadingSpinner />
      <span>{{ $t('views.EvidenceView.loading') }}</span>
    </div>

    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="load" />

    <EmptyState
      v-else-if="!hasLoaded"
      :title="$t('views.EvidenceView.prompt_title')"
      :description="$t('views.EvidenceView.prompt_description')"
    />

    <EmptyState
      v-else-if="rows.length === 0"
      :title="$t('views.EvidenceView.empty_title')"
      :description="$t('views.EvidenceView.empty_description')"
    />

    <div v-else class="space-y-3">
      <p aria-live="polite" data-testid="evidence-count" class="text-sm text-muted-foreground">
        {{ $t('views.EvidenceView.row_count', { count: rows.length }) }}
      </p>
      <p class="text-xs text-muted-foreground">{{ $t('views.EvidenceView.redaction_note') }}</p>
      <DataCard padding="none" elevation="sm" card-class="overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b bg-muted/50">
              <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.EvidenceView.col_key') }}</th>
              <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.EvidenceView.col_subject') }}</th>
              <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.EvidenceView.col_observed') }}</th>
              <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.EvidenceView.col_value') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" class="border-b last:border-b-0 align-top">
              <td class="px-3 py-2 font-mono text-xs">{{ row.key }}</td>
              <td class="px-3 py-2">
                <div>{{ row.subject_type }}</div>
                <div class="font-mono text-xs text-muted-foreground">{{ row.subject_id }}</div>
              </td>
              <td class="px-3 py-2 text-xs">{{ row.observed_at }}</td>
              <td class="px-3 py-2">
                <pre class="max-w-xl whitespace-pre-wrap break-words font-mono text-xs">{{ formatValue(row.value) }}</pre>
              </td>
            </tr>
          </tbody>
        </table>
      </DataCard>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import DataCard from '../components/ui/DataCard.vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { fetchRuns, type RunListItem } from '../lib/api/runs'

interface EvidenceRow {
  id: string
  organisation_id: string
  key: string
  subject_type: string
  subject_id: string
  value: unknown
  observed_at: string
  producer_type: string
  producer_id: string | null
  created_at: string
  updated_at: string
}

const { t } = useI18n()
const route = useRoute()

const runId = ref('')
const nodeId = ref('')
const keyFilter = ref('')
const runSearch = ref('')
const recentRuns = ref<RunListItem[]>([])
const recentRunsLoading = ref(false)
const recentRunsError = ref<string | null>(null)
const rows = ref<EvidenceRow[]>([])

const filteredRecentRuns = computed(() => {
  const query = runSearch.value.trim().toLowerCase()
  if (!query) return recentRuns.value
  return recentRuns.value.filter((run) => run.run_id.toLowerCase().includes(query) || (run.pipeline_name ?? '').toLowerCase().includes(query))
})
const loading = ref(false)
const hasLoaded = ref(false)
const loadError = ref<string | null>(null)

function runLabel(run: RunListItem): string {
  const pipeline = run.pipeline_name ?? t('views.EvidenceView.unknown_pipeline')
  const created = run.created_at ? new Date(run.created_at).toLocaleString() : t('views.EvidenceView.unknown_time')
  return `${pipeline} · ${run.status} · ${created} · ${run.run_id}`
}

async function loadRecentRuns(): Promise<void> {
  recentRunsLoading.value = true
  recentRunsError.value = null
  try {
    const response = await fetchRuns({ page: 1, page_size: 50 })
    recentRuns.value = response.items
  } catch (err) {
    recentRunsError.value = err instanceof Error ? err.message : String(err)
  } finally {
    recentRunsLoading.value = false
  }
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    return String(value)
  }
}

async function load(): Promise<void> {
  if (!runId.value) {
    loadError.value = t('views.EvidenceView.run_id_required')
    return
  }

  loading.value = true
  loadError.value = null

  try {
    const { data, error, response } = await api.GET('/api/v1/evidence', {
      params: {
        query: {
          run_id: runId.value,
          node_id: nodeId.value || undefined,
          key: keyFilter.value || undefined,
        },
      },
    })
    if (error || !response?.ok) {
      throw new Error(
        formatApiError(error) ||
          t('views.EvidenceView.load_failed', { status: response?.status ?? 0 }),
      )
    }
    // The payload is already redacted server-side — render it as-is, with no
    // client-side field filtering.
    rows.value = data ?? []
    hasLoaded.value = true
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadRecentRuns()
  const queryRunId = route.query.run_id
  if (typeof queryRunId === 'string' && queryRunId) {
    runId.value = queryRunId
    const queryNodeId = route.query.node_id
    if (typeof queryNodeId === 'string') nodeId.value = queryNodeId
    const queryKey = route.query.key
    if (typeof queryKey === 'string') keyFilter.value = queryKey
    void load()
  }
})
</script>
