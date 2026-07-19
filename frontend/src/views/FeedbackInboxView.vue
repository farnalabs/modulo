<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.FeedbackInboxView.feedback_inbox')" :subtitle="$t('views.FeedbackInboxView.review_and_resolve_pending_feedback_from_pipeline_evaluation')" data-testid="feedback-inbox-title" />

    <FilterBar
      :filters="[{ key: 'status', label: $t('views.FeedbackInboxView.all'), options: [
        { value: 'pending', label: $t('views.FeedbackInboxView.pending') },
        { value: 'routing', label: $t('views.FeedbackInboxView.routing') },
        { value: 'correcting', label: $t('views.FeedbackInboxView.correcting') },
        { value: 'resolved', label: $t('views.FeedbackInboxView.resolved') },
        { value: 'escalated', label: $t('views.FeedbackInboxView.escalated') },
      ]}]"
      :filter-values="{ status: statusFilter }"
      @update:filter="(key, value) => { if (key === 'status') { statusFilter = value; loadFeedback() } }"
    >
      <template #after>
        <div class="flex items-center gap-2">
          <Select v-model="pipelineFilter" @update:model-value="loadFeedback">
            <SelectTrigger data-testid="feedback-inbox-pipeline-select" aria-label="Pipeline" class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              <SelectValue :placeholder="$t('views.FeedbackInboxView.all_pipelines')" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</SelectItem>
            </SelectContent>
          </Select>
          <input aria-label="date"
            v-model="dateFrom"
            type="date"
            data-testid="feedback-inbox-date-from"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="loadFeedback"
          />
          <input aria-label="date"
            v-model="dateTo"
            type="date"
            data-testid="feedback-inbox-date-to"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="loadFeedback"
          />
        </div>
      </template>
    </FilterBar>

    <ErrorAlert v-if="pipelinesError" :message="pipelinesError" :on-retry="loadPipelines" />

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadFeedback" />

    <template v-else>
      <EmptyState
        v-if="records.length === 0"
        data-testid="feedback-inbox-empty"
        :title="$t('views.FeedbackInboxView.no_feedback_yet')"
        :description="$t('views.FeedbackInboxView.all_feedback_records_have_been_resolved_or_no_evaluations_ha')"
      />

      <div v-else class="space-y-2">
        <div
          v-for="record in records"
          :key="record.id"
          class="rounded-lg border bg-card shadow-sm"
        >
          <div
            data-testid="feedback-inbox-toggle-expand"
            class="flex cursor-pointer items-center gap-4 p-4"
            :class="{ 'border-b': expandedId === record.id }"
            role="button"
            tabindex="0"
            @click="toggleExpand(record.id)"
            @keydown.enter="toggleExpand(record.id)"
            @keydown.space.prevent="toggleExpand(record.id)"
          >
            <svg
              class="h-4 w-4 flex-shrink-0 text-muted-foreground transition-transform"
              :class="{ 'rotate-90': expandedId === record.id }"
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="m9 18 6-6-6-6" />
            </svg>

            <span :class="statusBadgeClass(record.feedback_status)" class="capitalize">
              {{ record.feedback_status }}
            </span>

            <div class="min-w-0 flex-1">
              <Tooltip :delay-duration="300">
                <TooltipTrigger as-child>
                  <p class="truncate text-sm font-medium">{{ record.pipeline_name }}</p>
                </TooltipTrigger>
                <TooltipContent side="top">
                  <p>{{ record.pipeline_name }}</p>
                </TooltipContent>
              </Tooltip>
            </div>

            <div class="min-w-0 flex-1">
              <Tooltip :delay-duration="300">
                <TooltipTrigger as-child>
                  <p class="truncate text-sm text-muted-foreground">{{ record.rejection_reason || '-' }}</p>
                </TooltipTrigger>
                <TooltipContent side="top" class="max-w-xs">
                  <p>{{ record.rejection_reason || '-' }}</p>
                </TooltipContent>
              </Tooltip>
            </div>

            <span class="flex-shrink-0 text-xs text-muted-foreground">
              {{ formatDate(record.created_at) }}
            </span>

            <span
              v-if="record.feedback_handler_type"
              class="inline-flex flex-shrink-0 items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
            >
              {{ handlerTypeLabel(record.feedback_handler_type) }}
            </span>
          </div>

          <div v-if="expandedId === record.id" class="border-t p-4">
            <div v-if="detailLoading[record.id]" class="flex items-center justify-center py-8">
              <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            </div>

            <template v-else-if="detailError[record.id]">
              <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
                {{ detailError[record.id] }}
                <button data-testid="feedback-inbox-retry" class="ml-2 underline" @click="loadDetail(record.id)">{{ $t('views.FeedbackInboxView.retry') }}</button>
              </div>
            </template>

            <template v-else-if="detailMap[record.id]">
              <div class="space-y-6">
                <div>
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">{{ $t('views.FeedbackInboxView.rejection_reason_heading') }}</h3>
                  <p class="text-sm">{{ detailMap[record.id].rejection_reason || $t('views.FeedbackInboxView.no_rejection_reason') }}</p>
                </div>

                <div>
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">{{ $t('views.FeedbackInboxView.rejected_output') }}</h3>
                  <pre class="max-h-64 overflow-auto rounded-lg bg-muted p-4 text-xs"><code>{{ formatJson(detailMap[record.id].rejected_output) }}</code></pre>
                </div>

                <div v-if="detailMap[record.id].correction_proposal">
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">{{ $t('views.FeedbackInboxView.correction_proposal') }}</h3>
                  <pre class="max-h-48 overflow-auto rounded-lg border border-primary/20 bg-primary/5 p-4 text-xs"><code>{{ formatJson(detailMap[record.id].correction_proposal) }}</code></pre>
                </div>

                <div v-if="detailMap[record.id].feedback_status === 'pending' || detailMap[record.id].feedback_status === 'routing'">
                  <Button
                    :disabled="triggering[record.id]"
                    data-testid="feedback-inbox-trigger-correction"
                    variant="default"
                    @click="triggerCorrection(record.id)"
                  >
                    {{ triggering[record.id] ? $t('views.FeedbackInboxView.triggering') : $t('views.FeedbackInboxView.trigger_correction_run') }}
                  </Button>
                </div>

                <div>
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">{{ $t('views.FeedbackInboxView.annotation_heading') }}</h3>
                  <textarea
                    v-model="annotations[record.id]"
                    rows="3"
                    maxlength="2000"
                    data-testid="feedback-inbox-annotation"
                    class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    :placeholder="$t('views.FeedbackInboxView.add_your_review_annotation')"
                  />
                  <div class="mt-2 flex items-center gap-2">
                    <Button
                      :disabled="savingAnnotation[record.id]"
                      data-testid="feedback-inbox-save-annotation"
                      variant="default"
                      @click="saveAnnotation(record.id)"
                    >
                      {{ savingAnnotation[record.id] ? $t('views.FeedbackInboxView.saving') : $t('views.FeedbackInboxView.save_annotation') }}
                    </Button>
                    <button
                      :disabled="savingAnnotation[record.id]"
                      data-testid="feedback-inbox-mark-resolved"
                      class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
                      @click="resolveRecord(record.id)"
                    >
                      {{ $t('views.FeedbackInboxView.mark_resolved') }}
                    </button>
                    <button
                      :disabled="dismissLoading[record.id]"
                      data-testid="feedback-inbox-dismiss"
                      class="rounded-lg border border-destructive/50 bg-background px-4 py-2 text-sm font-medium text-destructive hover:bg-destructive/10 disabled:opacity-50"
                      @click="dismissRecord(record.id)"
                    >
                      {{ dismissLoading[record.id] ? $t('views.FeedbackInboxView.dismissing') : $t('views.FeedbackInboxView.dismiss') }}
                    </button>
                    <div v-if="annotationMessage[record.id]" class="text-sm" :class="annotationMessage[record.id]?.type === 'error' ? 'text-destructive' : 'text-success'">
                      {{ annotationMessage[record.id]?.text }}
                    </div>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import type { components } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import { Button } from '@/components/ui/button'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '../components/ui/tooltip'
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select'
import { formatDateShortWithTime } from '../lib/formatDate'

interface FeedbackRecordItem {
  id: string
  created_at: string
  pipeline_name?: string | null
  rejection_reason?: string | null
  feedback_handler_type: string
  feedback_status: string
}

interface FeedbackRecordDetail extends FeedbackRecordItem {
  annotation?: string | null
  rejected_output?: unknown
  correction_proposal?: unknown
}

type PipelineItem = components['schemas']['PipelineResponse']

const { t } = useI18n()

const statusFilter = ref('')
const pipelineFilter = ref('')
const dateFrom = ref('')
const dateTo = ref('')

const expandedId = ref<string | null>(null)
const detailMap = ref<Record<string, FeedbackRecordDetail>>({})
const detailLoading = ref<Record<string, boolean>>({})
const detailError = ref<Record<string, string | null>>({})

const annotations = ref<Record<string, string>>({})
const savingAnnotation = ref<Record<string, boolean>>({})
const annotationMessage = ref<Record<string, { type: string; text: string } | null>>({})
const triggering = ref<Record<string, boolean>>({})
const dismissLoading = ref<Record<string, boolean>>({})
const feedbackTimeouts = ref<Record<string, ReturnType<typeof setTimeout>>>({})

function statusBadgeClass(status: string): string {
  const classMap: Record<string, string> = {
    pending: 'badge badge-status-pending',
    routing: 'badge badge-status-warning',
    correcting: 'badge badge-context-purple',
    resolved: 'badge badge-status-success',
    dismissed: 'badge badge-context-slate',
    escalated: 'badge badge-status-destructive',
  }
  return classMap[status] ?? 'badge badge-context-slate'
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return '-'
  return formatDateShortWithTime(d)
}

function handlerTypeLabel(type: string): string {
  const key = `views.FeedbackInboxView.handler_${type}`
  const label = t(key)
  return label !== key ? label : type
}

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

const { loading, error, data: feedbackResp, load: loadFeedback } = useDataFetch(
  async () => {
    const params: Record<string, string | number> = {}
    if (statusFilter.value) params.status = statusFilter.value
    if (pipelineFilter.value) params.pipeline_id = pipelineFilter.value
    if (dateFrom.value) params.date_from = dateFrom.value
    if (dateTo.value) params.date_to = dateTo.value
    return api.GET('/api/v1/feedback/inbox', {
      params: { query: params as any },
    })
  },
  { immediate: false },
)

const records = computed<FeedbackRecordItem[]>(() => {
  const response = feedbackResp.value as { items?: FeedbackRecordItem[] } | null
  return response?.items ?? []
})

const { error: pipelinesError, data: pipelinesResp, load: loadPipelines } = useDataFetch(
  () => api.GET('/api/v1/pipelines'),
)

const pipelines = computed<PipelineItem[]>(() => {
  const response = pipelinesResp.value as { items?: PipelineItem[] } | null
  return response?.items ?? []
})

async function loadDetail(recordId: string) {
  detailLoading.value[recordId] = true
  detailError.value[recordId] = null
  try {
    const { data, error: err } = await api.GET('/api/v1/feedback/inbox/{record_id}', {
      params: { path: { record_id: recordId } },
    })
    if (err) {
      detailError.value[recordId] = `${t('views.FeedbackInboxView.failed_to_load_detail')} ${formatApiError(err)}`
    } else if (data) {
      const detail = data as unknown as FeedbackRecordDetail
      detailMap.value[recordId] = detail
      annotations.value[recordId] = detail.annotation || ''
    }
  } catch (e: unknown) {
    detailError.value[recordId] = `${t('views.FeedbackInboxView.failed_to_load_detail')} ${formatApiError(e)}`
  } finally {
    detailLoading.value[recordId] = false
  }
}

function toggleExpand(recordId: string) {
  if (expandedId.value === recordId) {
    expandedId.value = null
  } else {
    expandedId.value = recordId
    if (!detailMap.value[recordId]) {
      loadDetail(recordId)
    }
  }
}

async function saveAnnotation(recordId: string) {
  savingAnnotation.value[recordId] = true
  annotationMessage.value[recordId] = null
  try {
    const { data, error: err } = await api.POST('/api/v1/feedback/inbox/{record_id}/review', {
      params: { path: { record_id: recordId } },
      body: { action: 'mark_reviewed', annotation: annotations.value[recordId] || null },
    })
    if (err) {
      annotationMessage.value[recordId] = { type: 'error', text: `${t('views.FeedbackInboxView.save_failed')} ${formatApiError(err)}` }
    } else if (data) {
      detailMap.value[recordId] = data as unknown as FeedbackRecordDetail
      annotationMessage.value[recordId] = { type: 'success', text: t('views.FeedbackInboxView.annotation_saved') }
      if (feedbackTimeouts.value[recordId]) clearTimeout(feedbackTimeouts.value[recordId])
      feedbackTimeouts.value[recordId] = setTimeout(() => { annotationMessage.value[recordId] = null }, 3000)
    }
  } catch (e: unknown) {
    annotationMessage.value[recordId] = { type: 'error', text: `${t('views.FeedbackInboxView.save_failed')} ${formatApiError(e)}` }
  } finally {
    savingAnnotation.value[recordId] = false
  }
}

async function resolveRecord(recordId: string) {
  savingAnnotation.value[recordId] = true
  annotationMessage.value[recordId] = null
  try {
    const { data, error: err } = await api.POST('/api/v1/feedback/inbox/{record_id}/review', {
      params: { path: { record_id: recordId } },
      body: { action: 'mark_reviewed', annotation: annotations.value[recordId] || null },
    })
    if (err) {
      annotationMessage.value[recordId] = { type: 'error', text: `${t('views.FeedbackInboxView.resolve_failed')} ${formatApiError(err)}` }
    } else if (data) {
      detailMap.value[recordId] = data as unknown as FeedbackRecordDetail
      annotationMessage.value[recordId] = { type: 'success', text: t('views.FeedbackInboxView.marked_as_resolved') }
      const rec = records.value.find(r => r.id === recordId)
      if (rec) rec.feedback_status = 'resolved'
      if (feedbackTimeouts.value[recordId]) clearTimeout(feedbackTimeouts.value[recordId])
      feedbackTimeouts.value[recordId] = setTimeout(() => { annotationMessage.value[recordId] = null }, 3000)
    }
  } catch (e: unknown) {
    annotationMessage.value[recordId] = { type: 'error', text: `${t('views.FeedbackInboxView.resolve_failed')} ${formatApiError(e)}` }
  } finally {
    savingAnnotation.value[recordId] = false
  }
}

async function triggerCorrection(recordId: string) {
  triggering.value[recordId] = true
  annotationMessage.value[recordId] = null
  try {
    const { data, error: err } = await api.POST('/api/v1/feedback/inbox/{record_id}/review', {
      params: { path: { record_id: recordId } },
      body: { action: 'create_correction_run', annotation: annotations.value[recordId] || null },
    })
    if (err) {
      annotationMessage.value[recordId] = { type: 'error', text: `${t('views.FeedbackInboxView.trigger_failed')} ${formatApiError(err)}` }
    } else if (data) {
      detailMap.value[recordId] = data as unknown as FeedbackRecordDetail
      annotationMessage.value[recordId] = { type: 'success', text: t('views.FeedbackInboxView.correction_run_triggered') }
      const rec = records.value.find(r => r.id === recordId)
      if (rec) rec.feedback_status = 'correcting'
      if (feedbackTimeouts.value[recordId]) clearTimeout(feedbackTimeouts.value[recordId])
      feedbackTimeouts.value[recordId] = setTimeout(() => { annotationMessage.value[recordId] = null }, 3000)
    }
  } catch (e: unknown) {
    annotationMessage.value[recordId] = { type: 'error', text: `${t('views.FeedbackInboxView.trigger_failed')} ${formatApiError(e)}` }
  } finally {
    triggering.value[recordId] = false
  }
}

async function dismissRecord(recordId: string) {
  dismissLoading.value[recordId] = true
  annotationMessage.value[recordId] = null
  try {
    const { data, error: err } = await api.POST('/api/v1/feedback/inbox/{record_id}/review', {
      params: { path: { record_id: recordId } },
      body: { action: 'dismiss', annotation: annotations.value[recordId] || null },
    })
    if (err) {
      annotationMessage.value[recordId] = { type: 'error', text: `${t('views.FeedbackInboxView.dismiss_failed')} ${formatApiError(err)}` }
    } else if (data) {
      detailMap.value[recordId] = data as unknown as FeedbackRecordDetail
      annotationMessage.value[recordId] = { type: 'success', text: t('views.FeedbackInboxView.dismissed') }
      const rec = records.value.find(r => r.id === recordId)
      if (rec) rec.feedback_status = 'dismissed'
      if (feedbackTimeouts.value[recordId]) clearTimeout(feedbackTimeouts.value[recordId])
      feedbackTimeouts.value[recordId] = setTimeout(() => { annotationMessage.value[recordId] = null }, 3000)
    }
  } catch (e: unknown) {
    annotationMessage.value[recordId] = { type: 'error', text: `${t('views.FeedbackInboxView.dismiss_failed')} ${formatApiError(e)}` }
  } finally {
    dismissLoading.value[recordId] = false
  }
}

onBeforeUnmount(() => {
  for (const tid of Object.values(feedbackTimeouts.value)) {
    if (tid) clearTimeout(tid)
  }
})

import { onMounted } from 'vue'
onMounted(async () => {
  await Promise.all([loadFeedback(), loadPipelines()])
})
</script>
