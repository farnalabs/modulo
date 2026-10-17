<template>
  <div class="page-narrow space-y-6">
    <PageHeader
      :title="$t('views.EvidenceView.title')"
      :subtitle="$t('views.EvidenceView.subtitle')"
    />

    <form class="flex flex-wrap items-end gap-3" @submit.prevent="load">
      <div>
        <label for="evidence-run-id" class="mb-1 block text-sm font-medium">
          {{ $t('views.EvidenceView.run_id') }}
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
      <div class="overflow-x-auto rounded-lg border bg-card shadow-sm">
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
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

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
const rows = ref<EvidenceRow[]>([])
const loading = ref(false)
const hasLoaded = ref(false)
const loadError = ref<string | null>(null)

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
