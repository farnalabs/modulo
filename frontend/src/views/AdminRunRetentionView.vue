<template>
  <FeatureGate feature-name="admin_run_retention" required-tier="team" show-disabled>
    <div class="page-wide">
      <PageHeader
        :title="$t('views.AdminRunRetentionView.run_retention')"
        :subtitle="$t('views.AdminRunRetentionView.page_subtitle')"
      >
        <template #right>
          <Button
            severity="secondary"
            outlined
            data-testid="admin-run-retention-refresh"
            :disabled="loading"
            @click="loadCandidates"
          >
            {{ loading ? $t('views.AdminRunRetentionView.refreshing') : $t('views.AdminRunRetentionView.refresh') }}
          </Button>
          <Button
            data-testid="admin-run-retention-export"
            :disabled="exporting || candidates.length === 0"
            @click="exportFile"
          >
            {{ exporting ? $t('views.AdminRunRetentionView.exporting') : $t('views.AdminRunRetentionView.export_to_file') }}
          </Button>
          <Button
            severity="danger"
            data-testid="admin-run-retention-purge"
            :disabled="purging || terminalCandidates.length === 0"
            @click="openPurgeConfirm"
          >
            {{ purging ? $t('views.AdminRunRetentionView.purging') : $t('views.AdminRunRetentionView.purge') }}
          </Button>
        </template>
      </PageHeader>

      <div
        v-if="exportError || purgeError"
        class="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
        data-testid="admin-run-retention-error"
      >
        {{ exportError || purgeError }}
      </div>
      <div
        v-if="exportSuccess"
        class="mb-4 rounded-lg border border-success/50 bg-success/10 p-3 text-sm text-success"
        data-testid="admin-run-retention-export-result"
      >
        {{ exportSuccess }}
      </div>
      <div
        v-if="purgeResult"
        class="mb-4 rounded-lg border border-success/50 bg-success/10 p-3 text-sm text-success"
        data-testid="admin-run-retention-purge-result"
      >
        {{ $t('views.AdminRunRetentionView.purge_result', { runs: purgeResult.purged_runs, checkpoints: purgeResult.purged_checkpoints, bytes: formatBytes(purgeResult.freed_estimated_bytes) }) }}
      </div>

      <Card>
        <template #title>{{ $t('views.AdminRunRetentionView.filters_title') }}</template>
        <template #subtitle>{{ $t('views.AdminRunRetentionView.filters_subtitle') }}</template>
        <template #content>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <label class="mb-1.5 block text-xs font-medium text-muted-foreground" for="rr-date-from">{{ $t('views.AdminRunRetentionView.from_label') }}</label>
              <input
                id="rr-date-from"
                v-model="dateFrom"
                type="datetime-local"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                data-testid="admin-run-retention-date-from"
              />
            </div>
            <div>
              <label class="mb-1.5 block text-xs font-medium text-muted-foreground" for="rr-date-to">{{ $t('views.AdminRunRetentionView.to_label') }}</label>
              <input
                id="rr-date-to"
                v-model="dateTo"
                type="datetime-local"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                data-testid="admin-run-retention-date-to"
              />
            </div>
            <div>
              <label class="mb-1.5 block text-xs font-medium text-muted-foreground" for="rr-pipeline">{{ $t('views.AdminRunRetentionView.pipeline_label') }}</label>
              <Select
                id="rr-pipeline"
                v-model="selectedPipelineId"
                :options="pipelineOptions"
                option-label="label"
                option-value="value"
                :placeholder="$t('views.AdminRunRetentionView.all_pipelines')"
                class="w-full"
                data-testid="admin-run-retention-pipeline"
              />
            </div>
            <div>
              <label class="mb-1.5 block text-xs font-medium text-muted-foreground" for="rr-status">{{ $t('views.AdminRunRetentionView.status_label') }}</label>
              <Select
                id="rr-status"
                v-model="selectedStatus"
                :options="statusOptions"
                option-label="label"
                option-value="value"
                :placeholder="$t('views.AdminRunRetentionView.all_statuses')"
                class="w-full"
                data-testid="admin-run-retention-status"
              />
            </div>
          </div>
          <div class="mt-3 flex items-center gap-2">
            <Button data-testid="admin-run-retention-apply" @click="loadCandidates">
              {{ $t('views.AdminRunRetentionView.apply_filters') }}
            </Button>
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              data-testid="admin-run-retention-reset"
              @click="resetFilters"
            >
              {{ $t('views.AdminRunRetentionView.reset') }}
            </button>
          </div>
        </template>
      </Card>

      <Card>
        <template #title>{{ $t('views.AdminRunRetentionView.summary_title') }}</template>
        <template #subtitle>{{ $t('views.AdminRunRetentionView.summary_subtitle') }}</template>
        <template #content>
          <div v-if="loading" class="flex justify-center py-6">
            <LoadingSpinner />
          </div>
          <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive" data-testid="admin-run-retention-error-msg">
            {{ error }}
          </div>
          <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div class="rounded-lg border bg-muted p-4 text-center">
              <p class="text-2xl font-semibold" data-testid="admin-run-retention-total-runs">{{ totalCount }}</p>
              <p class="text-xs text-muted-foreground">{{ $t('views.AdminRunRetentionView.matching_runs') }}</p>
            </div>
            <div class="rounded-lg border bg-muted p-4 text-center">
              <p class="text-2xl font-semibold" data-testid="admin-run-retention-total-bytes">{{ formatBytes(displayTerminalBytes) }}</p>
              <p class="text-xs text-muted-foreground">{{ $t('views.AdminRunRetentionView.estimated_reclaimable') }}</p>
            </div>
            <div class="rounded-lg border bg-muted p-4 text-center">
              <p class="text-2xl font-semibold" data-testid="admin-run-retention-terminal-runs">{{ displayTerminalCount }}</p>
              <p class="text-xs text-muted-foreground">{{ $t('views.AdminRunRetentionView.terminal_purgeable') }}</p>
            </div>
          </div>
        </template>
      </Card>

      <div class="mb-4 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm" data-testid="admin-run-retention-warning">
        {{ $t('views.AdminRunRetentionView.warning_terminal_only') }}
      </div>

      <Card>
        <template #title>{{ $t('views.AdminRunRetentionView.candidates_title') }}</template>
        <template #subtitle>{{ $t('views.AdminRunRetentionView.candidates_subtitle') }}</template>
        <template #content>
          <div v-if="loading" class="flex justify-center py-6">
            <LoadingSpinner />
          </div>
          <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ error }}
          </div>
            <div v-else-if="candidates.length === 0" class="py-6 text-center text-sm text-muted-foreground" data-testid="admin-run-retention-empty">
              {{ $t('views.AdminRunRetentionView.no_runs_match') }}
            </div>
          <div v-else class="max-h-96 overflow-auto rounded-lg border" data-testid="admin-run-retention-candidates-table">
            <table class="w-full">
              <thead>
                <tr>
                  <th class="table-header">{{ $t('views.AdminRunRetentionView.col_status') }}</th>
                  <th class="table-header">{{ $t('views.AdminRunRetentionView.col_created') }}</th>
                  <th class="table-header">{{ $t('views.AdminRunRetentionView.col_pipeline') }}</th>
                  <th class="table-header">{{ $t('views.AdminRunRetentionView.col_estimated_size') }}</th>
                  <th class="table-header">{{ $t('views.AdminRunRetentionView.col_run_id') }}</th>
                </tr>
              </thead>
              <tbody class="divide-y">
                <tr v-for="run in candidates" :key="run.id">
                  <td class="table-cell">
                    <span :class="statusBadge(run.status.toLowerCase())" class="rounded px-2 py-0.5 text-xs font-medium capitalize">{{ run.status }}</span>
                  </td>
                  <td class="table-cell whitespace-nowrap text-xs text-muted-foreground">{{ formatDateShort(run.created_at) }}</td>
                  <td class="table-cell text-xs">{{ pipelineName(run.pipeline_id) }}</td>
                  <td class="table-cell whitespace-nowrap text-xs">{{ formatBytes(run.estimated_bytes) }}</td>
                  <td class="table-cell whitespace-nowrap font-mono text-xs text-muted-foreground">{{ shortId(run.id) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </Card>

      <Dialog
        :visible="showPurgeConfirm"
        :modal="true"
        :dismissable-mask="true"
        data-testid="admin-run-retention-confirm-dialog"
        @update:visible="showPurgeConfirm = $event"
      >
        <template #header>
          <div>
            <div class="text-lg font-semibold">{{ $t('views.AdminRunRetentionView.confirm_clear_down') }}</div>
            <div class="mt-0.5 text-sm text-muted-foreground">
              {{ $t('views.AdminRunRetentionView.confirm_warning') }}
            </div>
          </div>
        </template>
        <div class="space-y-3 text-sm">
          <p>
            {{ $t('views.AdminRunRetentionView.confirm_purge_detail', { count: displayTerminalCount }) }}
          </p>
           <p class="text-muted-foreground">{{ $t('views.AdminRunRetentionView.estimated_reclaimable') }}: {{ formatBytes(displayTerminalBytes) }}.</p>
        </div>
        <template #footer>
          <div class="flex justify-end gap-2">
            <Button severity="secondary" outlined data-testid="admin-run-retention-purge-cancel" @click="showPurgeConfirm = false">
              {{ $t('views.AdminRunRetentionView.cancel') }}
            </Button>
            <Button severity="danger" :disabled="purging" data-testid="admin-run-retention-purge-confirm" @click="executePurge">
              {{ purging ? $t('views.AdminRunRetentionView.purging') : $t('views.AdminRunRetentionView.purge_selected') }}
            </Button>
          </div>
        </template>
      </Dialog>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import FeatureGate from '../components/FeatureGate.vue'
import Card from 'primevue/card'
import Select from 'primevue/select'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import { api, getAuthHeaders } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { isTerminalStatus } from '../constants/runStatuses'
import { RUN_STATUS } from '../constants/filters'
import { formatDateShort } from '../lib/formatDate'
import type { components } from '../lib/api/schema'

const { t } = useI18n()

type CandidatesResponse = components['schemas']['CandidatesResponse']
type RetentionCandidate = components['schemas']['RetentionCandidate']
type PurgeResponse = components['schemas']['PurgeResponse']
type PipelineResponse = components['schemas']['PipelineResponse']

const AVAILABLE_STATUSES = Object.values(RUN_STATUS)

const dateFrom = ref('')
const dateTo = ref('')
const selectedPipelineId = ref<string | null>(null)
const selectedStatus = ref<string | null>(null)

interface AppliedFilters {
  dateFrom: string
  dateTo: string
  pipelineId: string | null
  status: string | null
}

const appliedFilters = ref<AppliedFilters | null>(null)

const candidates = ref<RetentionCandidate[]>([])
const totalCount = ref(0)
const terminalTotal = ref(0)
const terminalEstimatedBytesResp = ref(0)
const pipelines = ref<PipelineResponse[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const exporting = ref(false)
const exportError = ref<string | null>(null)
const exportSuccess = ref<string | null>(null)

const purging = ref(false)
const purgeError = ref<string | null>(null)
const purgeResult = ref<PurgeResponse | null>(null)
const showPurgeConfirm = ref(false)

const pipelineOptions = computed(() => pipelines.value.map(p => ({ value: p.id, label: p.name })))
const statusOptions = computed(() => AVAILABLE_STATUSES.map(s => ({ value: s, label: s.replace(/_/g, ' ') })))

const terminalCandidates = computed(() => candidates.value.filter(c => isTerminalStatus(c.status.toLowerCase())))

// The purge deletes EVERY matching terminal run (unbounded), so the confirm
// count and reclaimable figure must come from the server-side terminal totals,
// not the page-capped candidate list the client holds. Fall back to the loaded
// page when the server response predates these fields.
const displayTerminalCount = computed(() =>
  terminalTotal.value || terminalCandidates.value.length,
)
const displayTerminalBytes = computed(() =>
  terminalEstimatedBytesResp.value ||
  terminalCandidates.value.reduce((sum, c) => sum + (c.estimated_bytes ?? 0), 0),
)

function toIso(value: string): string | null {
  if (!value) return null
  const d = new Date(value)
  return isNaN(d.getTime()) ? null : d.toISOString()
}

function buildQuery(): Record<string, unknown> {
  const f = appliedFilters.value ?? {
    dateFrom: dateFrom.value,
    dateTo: dateTo.value,
    pipelineId: selectedPipelineId.value,
    status: selectedStatus.value,
  }
  const q: Record<string, unknown> = { limit: 500 }
  const df = toIso(f.dateFrom)
  const dt = toIso(f.dateTo)
  if (df) q.date_from = df
  if (dt) q.date_to = dt
  if (f.pipelineId) q.pipeline_id = f.pipelineId
  if (f.status) q.status = f.status
  return q
}

function buildBody(): Record<string, unknown> {
  const f = appliedFilters.value ?? {
    dateFrom: dateFrom.value,
    dateTo: dateTo.value,
    pipelineId: selectedPipelineId.value,
    status: selectedStatus.value,
  }
  return {
    date_from: toIso(f.dateFrom),
    date_to: toIso(f.dateTo),
    pipeline_id: f.pipelineId,
    status: f.status,
  }
}

function resetFilters() {
  dateFrom.value = ''
  dateTo.value = ''
  selectedPipelineId.value = null
  selectedStatus.value = null
  loadCandidates()
}

async function loadPipelines() {
    const res = await api.GET('/api/v1/pipelines', { params: { query: { page_size: 100 } } })
  if (!res.error && res.data) {
    pipelines.value = res.data.items
  }
}

async function loadCandidates() {
  loading.value = true
  error.value = null
  purgeResult.value = null
  purgeError.value = null
  try {
    const res = await api.GET('/api/v1/admin/run-retention/candidates', {
      params: { query: buildQuery() as any },
    })
    if (res.error) throw new Error(formatApiError(res.error))
    const data: CandidatesResponse | undefined = res.data as CandidatesResponse | undefined
    candidates.value = data?.runs ?? []
    totalCount.value = data?.total_count ?? 0
    terminalTotal.value = data?.terminal_total ?? 0
    terminalEstimatedBytesResp.value = data?.terminal_estimated_bytes ?? 0
    appliedFilters.value = {
      dateFrom: dateFrom.value,
      dateTo: dateTo.value,
      pipelineId: selectedPipelineId.value,
      status: selectedStatus.value,
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : t('views.AdminRunRetentionView.failed_to_load_candidates')
  } finally {
    loading.value = false
  }
}

function openPurgeConfirm() {
  purgeError.value = null
  showPurgeConfirm.value = true
}

async function executePurge() {
  purging.value = true
  purgeError.value = null
  purgeResult.value = null
  try {
    const res = await api.POST('/api/v1/admin/run-retention/purge', {
      body: { ...buildBody(), confirm: true } as any,
    })
    if (res.error) throw new Error(formatApiError(res.error))
    showPurgeConfirm.value = false
    await loadCandidates()
    purgeResult.value = res.data as PurgeResponse
  } catch (e: unknown) {
    purgeError.value = e instanceof Error ? e.message : t('views.AdminRunRetentionView.purge_failed')
    showPurgeConfirm.value = false
  } finally {
    purging.value = false
  }
}

async function exportFile() {
  exporting.value = true
  exportError.value = null
  exportSuccess.value = null
  try {
    const res = await fetch('/api/v1/admin/run-retention/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
      body: JSON.stringify(buildBody()),
    })
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(formatApiError(detail) || `Export failed: ${res.status}`)
    }
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'run-retention-export.ndjson'
    a.click()
    URL.revokeObjectURL(url)
    exportSuccess.value = t('views.AdminRunRetentionView.export_downloaded')
  } catch (e: unknown) {
    exportError.value = e instanceof Error ? e.message : t('views.AdminRunRetentionView.export_failed')
  } finally {
    exporting.value = false
  }
}

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const val = bytes / Math.pow(1024, i)
  return `${val.toFixed(1)} ${units[i]}`
}

function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id
}

function pipelineName(id: string): string {
  const p = pipelines.value.find(p => p.id === id)
  return p ? p.name : shortId(id)
}

function statusBadge(status: string): string {
  if (status === 'complete') return 'bg-success/15 text-success'
  if (status === 'failed') return 'bg-destructive/15 text-destructive'
  if (status === 'cancelled' || status === 'stalled') return 'bg-muted text-muted-foreground'
  return 'bg-secondary text-secondary-foreground'
}

loadPipelines()
loadCandidates()
</script>
