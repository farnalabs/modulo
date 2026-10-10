<template>
  <FeatureGate feature-name="eval_system" required-tier="community" show-disabled>

    <div class="page-wide">
    <PageHeader :title="$t('views.EvalProposalsQueueView.title')" :subtitle="$t('views.EvalProposalsQueueView.subtitle')" />

    <div v-if="loading" class="space-y-4">
      <div v-for="i in 3" :key="i" class="card p-5 animate-pulse">
        <div class="flex items-start justify-between gap-4">
          <div class="flex-1 space-y-3">
            <div class="flex flex-wrap items-center gap-2">
              <div class="h-5 w-24 bg-muted rounded" />
              <div class="h-5 w-20 bg-muted rounded" />
            </div>
            <div class="h-4 w-1/3 bg-muted rounded" />
            <div class="h-3 w-2/3 bg-muted rounded" />
            <div class="h-3 w-1/2 bg-muted rounded" />
          </div>
          <div class="flex shrink-0 items-center gap-2">
            <div class="h-9 w-20 bg-muted rounded" />
            <div class="h-9 w-20 bg-muted rounded" />
          </div>
        </div>
      </div>
    </div>

    <ErrorAlert v-else-if="pageError" :message="pageError" :on-retry="loadProposals" />

    <template v-else>
      <EmptyState
        v-if="proposals.length === 0"
        :title="$t('views.EvalProposalsQueueView.empty_title')"
        :description="$t('views.EvalProposalsQueueView.empty_description')"
      />

      <div v-else class="space-y-4">
        <div
          v-for="p in proposals"
          :key="p.id"
          class="rounded-lg border bg-card p-5 shadow-sm"
          :data-testid="'proposal-card-' + p.id"
        >
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0 flex-1 space-y-3">
              <div class="flex flex-wrap items-center gap-2">
                <span class="inline-block rounded bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                  {{ p.pipeline_name || $t('views.EvalProposalsQueueView.unnamed_pipeline') }}
                </span>
                <span
                  class="inline-block rounded px-2 py-0.5 text-xs font-medium"
                  :class="statusBadgeClass(p.feedback_status)"
                >
                  {{ statusLabel(p.feedback_status) }}
                </span>
                <span v-if="p.run_id" class="text-xs text-muted-foreground font-mono">
                  {{ $t('views.EvalProposalsQueueView.run_prefix', { id: shortId(p.run_id) }) }}
                </span>
              </div>

              <div>
                <p class="text-sm font-medium text-foreground">{{ $t('views.EvalProposalsQueueView.gap_description') }}</p>
                <p class="mt-0.5 text-sm text-muted-foreground">{{ p.rejection_reason }}</p>
              </div>

              <div class="grid grid-cols-2 gap-4 text-xs text-muted-foreground">
                <div>
                  <span class="font-medium text-foreground">{{ $t('views.EvalProposalsQueueView.gate') }}</span> <span class="font-mono text-xs">{{ shortId(p.review_id) }}</span>
                </div>
                <div>
                  <span class="font-medium text-foreground">{{ $t('views.EvalProposalsQueueView.node') }}</span>
                  <span v-if="p.producing_node_name" class="text-muted-foreground">{{ p.producing_node_name }}</span>
                  <span v-else class="font-mono text-xs text-muted-foreground">{{ shortId(p.producing_node_id) }}</span>
                </div>
                <div>
                  <span class="font-medium text-foreground">{{ $t('views.EvalProposalsQueueView.detected') }}</span> {{ p.created_at ? formatDate(p.created_at) : '—' }}
                </div>
                <div v-if="p.needs_human_review">
                  <span class="font-medium text-amber-500">{{ $t('views.EvalProposalsQueueView.needs_human_review') }}</span>
                </div>
              </div>
            </div>

            <div v-if="isActionable(p.feedback_status)" class="flex shrink-0 items-center gap-2">
            <Button :disabled="actioningId === p.id" data-testid="proposal-publish" @click="openPublishDialog(p)">
              {{ actioningId === p.id ? $t('views.EvalProposalsQueueView.publishing') : $t('views.EvalProposalsQueueView.publish') }}
            </Button>
              <button type="button"
                :disabled="actioningId === p.id"
                data-testid="proposal-dismiss"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
                @click="dismissProposal(p.id)"
              >
                {{ actioningId === p.id ? $t('views.EvalProposalsQueueView.dismissing') : $t('views.EvalProposalsQueueView.dismiss') }}
              </button>
            </div>
          </div>

          <div
            v-if="actionMessages[p.id]"
            class="mt-3 text-sm"
            :class="actionMessages[p.id].type === 'error' ? 'text-destructive' : 'text-success'"
            :role="actionMessages[p.id].type === 'error' ? 'alert' : 'status'"
            :aria-live="actionMessages[p.id].type === 'error' ? 'assertive' : 'polite'"
          >
            {{ actionMessages[p.id].text }}
          </div>
        </div>
      </div>
    </template>

    <Dialog
      v-model:visible="publishDialogVisible"
      modal
      :header="$t('views.EvalProposalsQueueView.publish_dialog_title')"
      :style="{ width: '32rem' }"
      :draggable="false"
      :dismissable-mask="true"
      data-testid="publish-proposal-dialog"
      @hide="closePublishDialog"
    >
      <div v-if="publishTarget" class="space-y-4">
        <div>
          <label for="publish-proposal-name" class="mb-1 block text-sm font-medium">
            {{ $t('views.EvalProposalsQueueView.name_label') }}
          </label>
          <input
            id="publish-proposal-name"
            v-model="publishForm.name"
            type="text"
            maxlength="255"
            autocomplete="off"
            data-testid="publish-name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :placeholder="$t('views.EvalProposalsQueueView.name_placeholder')"
          />
        </div>

        <div>
          <span class="mb-1 block text-sm font-medium">
            {{ $t('views.EvalProposalsQueueView.eval_type_label') }}
          </span>
          <AppSelect
            input-id="publish-proposal-eval-type"
            v-model="publishForm.eval_type"
            :label="$t('views.EvalProposalsQueueView.eval_type_label')"
            data-testid="publish-eval-type"
            class="w-full"
            :options="evalTypeOptions"
            option-label="label"
            option-value="value"
          />
        </div>

        <div>
          <label for="publish-proposal-config" class="mb-1 block text-sm font-medium">
            {{ $t('views.EvalProposalsQueueView.config_label') }}
            <span class="text-muted-foreground">{{ $t('views.EvalProposalsQueueView.config_hint') }}</span>
          </label>
          <textarea
            id="publish-proposal-config"
            v-model="publishForm.config_json"
            rows="6"
            data-testid="publish-config"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :placeholder="publishConfigPlaceholder"
          />
          <div
            v-if="publishConfigError"
            class="mt-1 text-xs text-destructive"
            role="alert"
            aria-live="assertive"
          >
            {{ publishConfigError }}
          </div>
        </div>

        <div
          v-if="publishDialogError"
          class="text-sm text-destructive"
          role="alert"
          aria-live="assertive"
          data-testid="publish-dialog-error"
        >
          {{ publishDialogError }}
        </div>
      </div>

      <template #footer>
        <div v-if="publishTarget" class="flex justify-end gap-2">
          <Button
            severity="secondary"
            outlined
            :disabled="publishSubmitting"
            data-testid="publish-cancel"
            @click="closePublishDialog"
          >
            {{ $t('common.cancel') }}
          </Button>
          <Button
            :disabled="!canSubmitPublish"
            :loading="publishSubmitting"
            data-testid="publish-confirm"
            @click="submitPublish"
          >
            {{ $t('views.EvalProposalsQueueView.publish_confirm') }}
          </Button>
        </div>
      </template>
    </Dialog>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError, throwOnError } from '../lib/api/formatError'
import { shortId } from '../utils/format'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import AppSelect from '../components/shared/AppSelect.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { formatDateShortWithTime } from '../lib/formatDate'


interface EvalProposalItem {
  id: string
  run_id: string | null
  review_id: string
  rejected_by: string | null
  rejection_reason: string
  rejected_output: Record<string, unknown>
  producing_node_id: string
  producing_node_name: string | null
  producing_agent_id: string | null
  feedback_status: string
  feedback_handler_type: string
  correction_run_id: string | null
  eval_gap: boolean | null
  needs_human_review: boolean
  pipeline_name: string | null
  created_at: string | null
}

interface ProposalsResponse {
  items: EvalProposalItem[]
  total: number
  page: number
  page_size: number
}

const { t } = useI18n()

const { loading, error: pageError, data: proposalsResp, load: loadProposals } = useDataFetch<ProposalsResponse>(
  async () => {
    const response = await api.GET('/api/v1/feedback/proposals')
    return { data: response.data as unknown as ProposalsResponse | undefined, error: response.error }
  },
  { initialValue: { items: [] as EvalProposalItem[], total: 0, page: 1, page_size: 20 } },
)

const proposals = computed(() => proposalsResp.value?.items ?? [])
const actioningId = ref<string | null>(null)
const actionMessages = ref<Record<string, { type: string; text: string }>>({})

// --- Publish dialog state -------------------------------------------------
// FeedbackRecord stores no proposed-eval fields, so the reviewer supplies the
// eval definition (name, type, config) at publish time. These fields feed the
// POST /feedback/proposals/{id}/publish request body verbatim.
// Single source of truth for the publish dialog's eval_type enum. The select
// options and the submit guard below both derive from it, so a backend enum
// addition is a one-line change here instead of a silent drift. Option labels
// stay i18n-driven: the locale key is `views.EvalProposalsQueueView.<value>`,
// which matches each enum value by name.
const EVAL_TYPES = ['llm_judge', 'regex', 'json_schema', 'custom_function'] as const
type EvalType = (typeof EVAL_TYPES)[number]
const DEFAULT_EVAL_TYPE: EvalType = EVAL_TYPES[0]

const publishDialogVisible = ref(false)
const publishTarget = ref<EvalProposalItem | null>(null)
const publishSubmitting = ref(false)
const publishDialogError = ref<string | null>(null)
const publishForm = reactive<{ name: string; eval_type: EvalType; config_json: string }>({
  name: '',
  eval_type: DEFAULT_EVAL_TYPE,
  config_json: '{}',
})

const evalTypeOptions = computed(() =>
  EVAL_TYPES.map((value) => ({
    value,
    label: t(`views.EvalProposalsQueueView.${value}`),
  })),
)

const publishConfigPlaceholder = computed(() =>
  t(`views.EvalProposalsQueueView.config_placeholders.${publishForm.eval_type || DEFAULT_EVAL_TYPE}`),
)

const publishConfigError = computed<string | null>(() => {
  const raw = publishForm.config_json.trim()
  if (!raw) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return t('views.EvalProposalsQueueView.config_invalid')
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return t('views.EvalProposalsQueueView.config_must_be_object')
  }
  return null
})

const canSubmitPublish = computed(
  () =>
    publishForm.name.trim().length > 0 &&
    EVAL_TYPES.includes(publishForm.eval_type) &&
    !publishConfigError.value,
)

// A node id must be a real UUID before it is sent; the backend rejects a
// non-UUID with a 422. When the producing node id is not a UUID the field is
// omitted and the backend resolves the node from the record context.
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function isUuid(value: string | null | undefined): value is string {
  return typeof value === 'string' && UUID_RE.test(value)
}

function statusBadgeClass(status: string): string {
  const classMap: Record<string, string> = {
    pending: 'bg-pending/10 text-pending',
    routing: 'bg-warning/10 text-warning-text',
    correcting: 'bg-purple-100 text-purple-700',
    resolved: 'bg-success/10 text-success',
    escalated: 'bg-destructive/10 text-destructive',
    dismissed: 'bg-muted text-muted-foreground',
  }
  return classMap[status] ?? 'bg-muted text-muted-foreground'
}

function statusLabel(status: string): string {
  const key = `views.EvalProposalsQueueView.status_${status}`
  const translated = t(key)
  return translated === key ? status : translated
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  return formatDateShortWithTime(d)
}

function isActionable(status: string): boolean {
  return status === 'pending' || status === 'routing'
}

function openPublishDialog(p: EvalProposalItem) {
  publishTarget.value = p
  publishForm.name = ''
  publishForm.eval_type = DEFAULT_EVAL_TYPE
  publishForm.config_json = '{}'
  publishDialogError.value = null
  publishDialogVisible.value = true
}

function closePublishDialog() {
  publishDialogVisible.value = false
  publishTarget.value = null
  publishDialogError.value = null
}

async function submitPublish() {
  const target = publishTarget.value
  if (!target || !canSubmitPublish.value) return

  const raw = publishForm.config_json.trim()
  let configParsed: Record<string, unknown> = {}
  try {
    configParsed = raw ? JSON.parse(raw) : {}
  } catch {
    publishDialogError.value = t('views.EvalProposalsQueueView.config_invalid')
    return
  }

  const body: {
    name: string
    eval_type: string
    config: Record<string, unknown>
    node_id?: string
  } = {
    name: publishForm.name.trim(),
    eval_type: publishForm.eval_type,
    config: configParsed,
  }
  if (isUuid(target.producing_node_id)) {
    body.node_id = target.producing_node_id
  }

  publishSubmitting.value = true
  publishDialogError.value = null
  delete actionMessages.value[target.id]
  try {
    await throwOnError(
      await api.POST('/api/v1/feedback/proposals/{record_id}/publish', {
        params: { path: { record_id: target.id } },
        body,
      }),
    )
    actionMessages.value[target.id] = {
      type: 'success',
      text: t('views.EvalProposalsQueueView.publish_success'),
    }
    // Refresh so the published record surfaces its resolved status from the
    // server rather than relying on an optimistic client-side patch.
    await loadProposals()
    publishDialogVisible.value = false
    publishTarget.value = null
    setTimeout(() => { delete actionMessages.value[target.id] }, 5000)
  } catch (e: unknown) {
    // Surface the failure in the dialog (where the reviewer's attention is)
    // and persist it on the row so it remains visible after the dialog closes.
    const text = `${t('views.EvalProposalsQueueView.publish_failed')} ${formatApiError(e)}`
    publishDialogError.value = text
    actionMessages.value[target.id] = { type: 'error', text }
  } finally {
    publishSubmitting.value = false
  }
}

async function dismissProposal(id: string) {
  actioningId.value = id
  delete actionMessages.value[id]
  try {
    await throwOnError(
      await api.PATCH('/api/v1/feedback/{record_id}/status', {
        params: { path: { record_id: id } },
        body: { status: 'dismissed' },
      }),
    )
    actionMessages.value[id] = { type: 'success', text: t('views.EvalProposalsQueueView.dismissed') }
    // Deep-readonly query data: whole-response reassignment, never in-place.
    if (proposalsResp.value) {
      proposalsResp.value = {
        ...proposalsResp.value,
        items: proposalsResp.value.items.map(x =>
          x.id === id ? { ...x, feedback_status: 'dismissed' } : x),
      }
    }
    setTimeout(() => { delete actionMessages.value[id] }, 3000)
  } catch (e: unknown) {
    actionMessages.value[id] = { type: 'error', text: `${t('views.EvalProposalsQueueView.dismiss_failed')} ${formatApiError(e)}` }
  } finally {
    actioningId.value = null
  }
}
</script>
