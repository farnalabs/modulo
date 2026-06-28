<template>
  <div class="mx-auto max-w-6xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Feedback Inbox</h1>
      <p class="mt-1 text-muted-foreground">Review and resolve pending feedback from pipeline evaluations</p>
    </header>

    <div class="flex flex-wrap items-center gap-4">
      <div class="flex items-center gap-2">
        <label class="text-sm font-medium text-muted-foreground">Status</label>
        <select
          v-model="statusFilter"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @change="loadFeedback"
        >
          <option value="">All</option>
          <option value="pending">Pending</option>
          <option value="routing">Routing</option>
          <option value="correcting">Correcting</option>
          <option value="resolved">Resolved</option>
          <option value="escalated">Escalated</option>
        </select>
      </div>

      <div class="flex items-center gap-2">
        <label class="text-sm font-medium text-muted-foreground">Pipeline</label>
        <select
          v-model="pipelineFilter"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @change="loadFeedback"
        >
          <option value="">All Pipelines</option>
          <option
            v-for="p in pipelines"
            :key="p.id"
            :value="p.id"
          >
            {{ p.name }}
          </option>
        </select>
      </div>

      <div class="flex items-center gap-2">
        <label class="text-sm font-medium text-muted-foreground">From</label>
        <input
          v-model="dateFrom"
          type="date"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @change="loadFeedback"
        />
      </div>

      <div class="flex items-center gap-2">
        <label class="text-sm font-medium text-muted-foreground">To</label>
        <input
          v-model="dateTo"
          type="date"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @change="loadFeedback"
        />
      </div>
    </div>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" />

    <template v-else>
      <div v-if="records.length === 0" class="rounded-lg border bg-card p-8 text-center">
        <svg
          class="mx-auto mb-3 h-12 w-12 text-muted-foreground"
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
        >
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
        <p class="text-lg font-medium">No pending feedback</p>
        <p class="mt-1 text-sm text-muted-foreground">All feedback records have been resolved or no evaluations have run yet.</p>
      </div>

      <div v-else class="space-y-2">
        <div
          v-for="record in records"
          :key="record.id"
          class="rounded-lg border bg-card shadow-sm"
        >
          <div
            class="flex cursor-pointer items-center gap-4 p-4"
            :class="{ 'border-b': expandedId === record.id }"
            @click="toggleExpand(record.id)"
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

            <span :class="statusBadgeClass(record.status)">
              {{ record.status }}
            </span>

            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-medium">{{ record.pipeline_name }}</p>
            </div>

            <div class="min-w-0 flex-1">
              <p class="truncate text-sm text-muted-foreground">
                {{ record.rejection_reason || record.summary || '-' }}
              </p>
            </div>

            <span class="flex-shrink-0 text-xs text-muted-foreground">
              {{ formatDate(record.created_at) }}
            </span>

            <span
              v-if="record.handler_type"
              class="inline-flex flex-shrink-0 items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
            >
              {{ record.handler_type }}
            </span>
          </div>

          <div v-if="expandedId === record.id" class="border-t p-4">
            <div v-if="detailLoading[record.id]" class="flex items-center justify-center py-8">
              <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            </div>

            <template v-else-if="detailError[record.id]">
              <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
                {{ detailError[record.id] }}
                <button class="ml-2 underline" @click="loadDetail(record.id)">Retry</button>
              </div>
            </template>

            <template v-else-if="detailMap[record.id]">
              <div class="space-y-6">
                <div>
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">Rejection Reason</h3>
                  <p class="text-sm">{{ detailMap[record.id].rejection_reason || 'No rejection reason provided.' }}</p>
                </div>

                <div>
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">Rejected Output</h3>
                  <pre class="max-h-64 overflow-auto rounded-lg bg-muted p-4 text-xs"><code>{{ formatJson(detailMap[record.id].rejected_output) }}</code></pre>
                </div>

                <div v-if="detailMap[record.id].correction_proposal">
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">Correction Proposal</h3>
                  <pre class="max-h-48 overflow-auto rounded-lg border border-primary/20 bg-primary/5 p-4 text-xs"><code>{{ formatJson(detailMap[record.id].correction_proposal) }}</code></pre>
                </div>

                <div v-if="detailMap[record.id].status === 'pending' || detailMap[record.id].status === 'routing'">
                  <button
                    :disabled="triggering[record.id]"
                    class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                    @click="triggerCorrection(record.id)"
                  >
                    {{ triggering[record.id] ? 'Triggering...' : 'Trigger Correction Run' }}
                  </button>
                </div>

                <div>
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">Annotation</h3>
                  <textarea
                    v-model="annotations[record.id]"
                    rows="3"
                    class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    placeholder="Add your review annotation..."
                  />
                  <div class="mt-2 flex items-center gap-2">
                    <button
                      :disabled="savingAnnotation[record.id]"
                      class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                      @click="saveAnnotation(record.id)"
                    >
                      {{ savingAnnotation[record.id] ? 'Saving...' : 'Save Annotation' }}
                    </button>
                    <button
                      :disabled="savingAnnotation[record.id]"
                      class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
                      @click="resolveRecord(record.id)"
                    >
                      Mark Resolved
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
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

type FeedbackRecordItem = components['schemas']['FeedbackRecordItem']
type FeedbackRecordDetail = components['schemas']['FeedbackRecordDetail']
type PipelineItem = components['schemas']['PipelineItem']

const records = ref<FeedbackRecordItem[]>([])
const pipelines = ref<PipelineItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

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

function statusBadgeClass(status: string): string {
  const classMap: Record<string, string> = {
    pending: 'badge badge-status-pending',
    routing: 'badge badge-status-warning',
    correcting: 'badge badge-context-purple',
    resolved: 'badge badge-status-success',
    escalated: 'badge badge-status-destructive',
  }
  return classMap[status] ?? 'badge badge-context-slate'
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

async function loadPipelines() {
  try {
    const { data, error: err } = await api.GET('/api/v1/pipelines')
    if (!err && data) {
      pipelines.value = data.items
    }
  } catch {
    // Non-critical
  }
}

async function loadFeedback() {
  loading.value = true
  error.value = null
  try {
    const params: Record<string, string | number> = {}
    if (statusFilter.value) params.status = statusFilter.value
    if (pipelineFilter.value) params.pipeline_id = pipelineFilter.value
    if (dateFrom.value) params.date_from = dateFrom.value
    if (dateTo.value) params.date_to = dateTo.value

    const { data, error: err } = await api.GET('/api/v1/feedback/inbox', {
      params: { query: params as any },
    })
    if (err) {
      error.value = `Failed to load feedback: ${err}`
    } else if (data) {
      records.value = data.items
    }
  } catch (e: unknown) {
    error.value = `Failed to load feedback: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

async function loadDetail(recordId: string) {
  detailLoading.value[recordId] = true
  detailError.value[recordId] = null
  try {
    const { data, error: err } = await api.GET('/api/v1/feedback/inbox/{record_id}', {
      params: { path: { record_id: recordId } },
    })
    if (err) {
      detailError.value[recordId] = `Failed to load detail: ${err}`
    } else if (data) {
      detailMap.value[recordId] = data
      annotations.value[recordId] = data.annotation || ''
    }
  } catch (e: unknown) {
    detailError.value[recordId] = `Failed to load detail: ${e instanceof Error ? e.message : String(e)}`
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
      body: { annotation: annotations.value[recordId] || null },
    })
    if (err) {
      annotationMessage.value[recordId] = { type: 'error', text: `Save failed: ${err}` }
    } else if (data) {
      detailMap.value[recordId] = data
      annotationMessage.value[recordId] = { type: 'success', text: 'Annotation saved.' }
      setTimeout(() => { annotationMessage.value[recordId] = null }, 3000)
    }
  } catch (e: unknown) {
    annotationMessage.value[recordId] = { type: 'error', text: `Save failed: ${e instanceof Error ? e.message : String(e)}` }
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
      body: { status: 'resolved', annotation: annotations.value[recordId] || null },
    })
    if (err) {
      annotationMessage.value[recordId] = { type: 'error', text: `Resolve failed: ${err}` }
    } else if (data) {
      detailMap.value[recordId] = data
      annotationMessage.value[recordId] = { type: 'success', text: 'Marked as resolved.' }
      const rec = records.value.find(r => r.id === recordId)
      if (rec) rec.status = 'resolved'
      setTimeout(() => { annotationMessage.value[recordId] = null }, 3000)
    }
  } catch (e: unknown) {
    annotationMessage.value[recordId] = { type: 'error', text: `Resolve failed: ${e instanceof Error ? e.message : String(e)}` }
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
      body: { status: 'correcting', annotation: annotations.value[recordId] || null },
    })
    if (err) {
      annotationMessage.value[recordId] = { type: 'error', text: `Trigger failed: ${err}` }
    } else if (data) {
      detailMap.value[recordId] = data
      annotationMessage.value[recordId] = { type: 'success', text: 'Correction run triggered.' }
      const rec = records.value.find(r => r.id === recordId)
      if (rec) rec.status = 'correcting'
      setTimeout(() => { annotationMessage.value[recordId] = null }, 3000)
    }
  } catch (e: unknown) {
    annotationMessage.value[recordId] = { type: 'error', text: `Trigger failed: ${e instanceof Error ? e.message : String(e)}` }
  } finally {
    triggering.value[recordId] = false
  }
}

onMounted(async () => {
  await Promise.all([loadFeedback(), loadPipelines()])
})
</script>
