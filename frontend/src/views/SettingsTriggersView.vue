<template>
  <FeatureGate feature-name="webhook_trigger" required-tier="community" show-disabled>

    <div data-theme="agent" class="page-wide">
    <header class="flex items-center justify-between">
      <PageHeader title="Triggers" subtitle="Automate pipeline execution with schedules (cron), webhooks, polling, or inter-pipeline signals. Each trigger launches a pipeline run when its conditions are met." />
      <Button
        data-testid="settings-triggers-create"
        variant="default"
           class="border-primary/30 hover:border-primary/60"
        @click="openCreateDialog"
      >
        Create Trigger
      </Button>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadAll" />

    <div v-else-if="items.length === 0" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">No triggers configured</p>
      <p class="mt-1 text-sm text-muted-foreground">
        Create a trigger to automatically start pipeline runs on a schedule, webhook, or other event.
      </p>
    </div>

    <template v-else>
      <div class="overflow-hidden rounded-lg border bg-card shadow-sm">
        <table class="w-full text-left text-sm">
          <thead class="bg-muted/50 text-xs font-medium uppercase text-muted-foreground">
            <tr>
              <th class="px-4 py-3">Pipeline</th>
              <th class="px-4 py-3">Type</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Last Fired</th>
              <th class="px-4 py-3">Next Fire</th>
              <th class="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="t in items"
              :key="t.id"
              class="transition-colors hover:bg-muted/30"
            >
              <td class="px-4 py-3 font-medium">
                {{ pipelineName(t.pipeline_id) }}
              </td>
              <td class="px-4 py-3">
                <span :class="typeBadgeClass(t.trigger_type)" class="badge">
                  {{ typeLabel(t.trigger_type) }}
                </span>
              </td>
              <td class="px-4 py-3">
                <button
                  class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors disabled:opacity-50"
                  :class="t.active ? 'bg-success/10 text-success hover:bg-success/20' : 'bg-muted text-muted-foreground hover:bg-muted/80'"
                  :disabled="triggerToggling[t.id]"
                  data-testid="settings-triggers-toggle"
                  @click="toggleActive(t)"
                >
                  <span
                    class="h-1.5 w-1.5 rounded-full"
                    :class="t.active ? 'bg-success' : 'bg-muted-foreground'"
                  />
                  {{ triggerToggling[t.id] ? '...' : (t.active ? 'Active' : 'Inactive') }}
                </button>
              </td>
              <td class="px-4 py-3 text-muted-foreground">
                {{ formatTimestamp(t.last_fired_at ?? null) }}
              </td>
              <td class="px-4 py-3 text-muted-foreground">
                {{ formatTimestamp(t.next_fire_at ?? null) }}
              </td>
              <td class="px-4 py-3 text-right">
                <TableActions :actions="triggerActions(t)" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <FormDialog
      :open="dialogOpen"
      @update:open="dialogOpen = false"
      :title="editingId ? 'Edit Trigger' : 'Create Trigger'"
      :description="editingId ? 'Update the trigger configuration.' : 'Configure a new trigger for your pipeline.'"
      :confirmText="editingId ? 'Update' : 'Create'"
      :loading="saving"
      @confirm="saveTrigger"
    >
      <form @submit.prevent="saveTrigger" class="space-y-4">
        <div>
          <label for="settingstriggersview-pipeline" class="mb-1 block text-sm font-medium">Pipeline</label>
          <Select v-model="form.pipeline_id">
            <SelectTrigger id="settingstriggersview-pipeline" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm" aria-label="Pipeline" data-testid="settings-triggers-form-pipeline">
              <SelectValue placeholder="Select pipeline" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div v-if="!editingId">
          <label for="settingstriggersview-trigger-type" class="mb-1 block text-sm font-medium">Trigger Type</label>
          <Select v-model="form.trigger_type">
            <SelectTrigger id="settingstriggersview-trigger-type" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm" aria-label="Trigger type" data-testid="settings-triggers-form-type">
              <SelectValue placeholder="Select type" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="webhook">Webhook</SelectItem>
              <SelectItem value="cron">Cron</SelectItem>
              <SelectItem value="polling">Polling</SelectItem>
              <SelectItem value="agent_signal">Agent Signal</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div v-else class="text-sm text-muted-foreground">
          Type: <span class="font-medium">{{ typeLabel(editingType) }}</span>
        </div>

        <!-- Webhook config -->
        <template v-if="form.trigger_type === 'webhook'">
          <div>
            <label for="settingstriggersview-field-13" class="mb-1 block text-sm font-medium">URL</label>
            <input id="settingstriggersview-field-13"
              v-model="form.webhook_url"
              type="url"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="https://example.com/webhook"
              data-testid="settings-triggers-form-webhook-url"
            />
          </div>
          <div>
            <label for="settingstriggersview-http-method" class="mb-1 block text-sm font-medium">HTTP Method</label>
            <Select v-model="form.webhook_method">
              <SelectTrigger id="settingstriggersview-http-method" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm" aria-label="HTTP method" data-testid="settings-triggers-form-webhook-method">
                <SelectValue placeholder="Select method" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="POST">POST</SelectItem>
                <SelectItem value="GET">GET</SelectItem>
                <SelectItem value="PUT">PUT</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div>
            <label for="settingstriggersview-field-11" class="mb-1 block text-sm font-medium">Headers (JSON)</label>
            <textarea id="settingstriggersview-field-11"
              v-model="form.webhook_headers"
              rows="3"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              placeholder='{ "X-Custom-Header": "value" }'
              data-testid="settings-triggers-form-webhook-headers"
            />
          </div>
        </template>

        <!-- Cron config -->
        <template v-if="form.trigger_type === 'cron'">
          <div>
            <label for="settingstriggersview-field-10" class="mb-1 block text-sm font-medium">Cron Expression</label>
            <input id="settingstriggersview-field-10"
              v-model="form.cron_expression"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
              placeholder="*/5 * * * *"
              data-testid="settings-triggers-form-cron-expr"
            />
          </div>
          <div>
            <label for="settingstriggersview-field-9" class="mb-1 block text-sm font-medium">Timezone</label>
            <input id="settingstriggersview-field-9"
              v-model="form.cron_timezone"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="UTC"
              data-testid="settings-triggers-form-cron-tz"
            />
          </div>
          <div>
            <label for="settingstriggersview-field-8" class="mb-1 block text-sm font-medium">Input Template (JSON)</label>
            <textarea id="settingstriggersview-field-8"
              v-model="form.input_template"
              rows="3"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              placeholder='{ "key": "value" }'
              data-testid="settings-triggers-form-cron-input"
            />
          </div>
        </template>

        <!-- Polling config -->
        <template v-if="form.trigger_type === 'polling'">
          <div>
            <label for="settingstriggersview-field-7" class="mb-1 block text-sm font-medium">Connector Instance ID</label>
            <input id="settingstriggersview-field-7"
              v-model="form.connector_instance_id"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
              placeholder="uuid"
              data-testid="settings-triggers-form-polling-connector"
            />
          </div>
          <div>
            <label for="settingstriggersview-field-6" class="mb-1 block text-sm font-medium">Query</label>
            <textarea id="settingstriggersview-field-6"
              v-model="form.poll_query"
              rows="3"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              placeholder="SELECT * FROM table WHERE status = 'pending'"
              data-testid="settings-triggers-form-polling-query"
            />
          </div>
          <div>
            <label for="settingstriggersview-field-5" class="mb-1 block text-sm font-medium">Poll Interval (seconds)</label>
            <input id="settingstriggersview-field-5"
              v-model="form.poll_interval"
              type="number"
              min="10"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="60"
              data-testid="settings-triggers-form-polling-interval"
            />
          </div>
          <div>
            <label for="settingstriggersview-field-4" class="mb-1 block text-sm font-medium">Condition Expression (JMESPath)</label>
            <input id="settingstriggersview-field-4"
              v-model="form.condition_expression"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
              placeholder="length(@) > `0`"
              data-testid="settings-triggers-form-polling-condition"
            />
          </div>
        </template>

        <!-- Agent Signal config -->
        <template v-if="form.trigger_type === 'agent_signal'">
          <div>
            <label for="settingstriggersview-source-pipeline" class="mb-1 block text-sm font-medium">Source Pipeline</label>
            <Select v-model="form.signal_source_pipeline">
              <SelectTrigger id="settingstriggersview-source-pipeline" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm" aria-label="Source pipeline" data-testid="settings-triggers-form-signal-pipeline">
                <SelectValue placeholder="Select source pipeline" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div>
            <label for="settingstriggersview-field-2" class="mb-1 block text-sm font-medium">Source Node ID</label>
            <input id="settingstriggersview-field-2"
              v-model="form.signal_source_node"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
              placeholder="node_abc123"
              data-testid="settings-triggers-form-signal-node"
            />
          </div>
        </template>

        <div class="flex items-center gap-2">
          <label for="settingstriggersview-field-1" class="flex items-center gap-2 text-sm">
            <input id="settingstriggersview-field-1"
              v-model="form.active"
              type="checkbox"
              class="rounded border-input"
              data-testid="settings-triggers-form-active"
            />
            Active
          </label>
        </div>

        <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
      </form>
    </FormDialog>

    <FormDialog
      :open="deleteDialogOpen"
      @update:open="deleteDialogOpen = false"
      title="Delete Trigger"
      description="Are you sure you want to delete this trigger? This action cannot be undone."
      confirmText="Delete"
      :loading="deleting"
      @confirm="deleteTrigger"
    />
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useDataFetch } from '../composables/useDataFetch'
import { Button } from '@/components/ui/button'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FormDialog from '../components/shared/FormDialog.vue'
import TableActions from '../components/shared/TableActions.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { shortId } from '../utils/format'
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from '@/components/ui/select'

const planStore = usePlanStore()

interface TriggerItem {
  id: string
  pipeline_id: string
  trigger_type: string
  active: boolean
  config_json: Record<string, unknown>
  cron_expression?: string | null
  cron_timezone?: string | null
  last_fired_at?: string | null
  next_fire_at?: string | null
}
type PipelineItem = components['schemas']['PipelineResponse']

interface TriggerForm {
  pipeline_id: string
  trigger_type: string
  active: boolean
  webhook_url: string
  webhook_method: string
  webhook_headers: string
  cron_expression: string
  cron_timezone: string
  input_template: string
  connector_instance_id: string
  poll_query: string
  poll_interval: number
  condition_expression: string
  signal_source_pipeline: string
  signal_source_node: string
}

const { loading, error, data: triggersData, load: loadTriggers } = useDataFetch(
  () => api.GET('/api/v1/triggers', { params: { query: { page: 1, page_size: 100 } } }),
)
const { data: pipelinesData, load: loadPipelines } = useDataFetch(
  () => api.GET('/api/v1/pipelines', {}),
  { immediate: false }
)
const items = computed<TriggerItem[]>(() =>
  ((triggersData.value as { items?: TriggerItem[] } | null)?.items ?? []),
)
const pipelines = computed<PipelineItem[]>(() =>
  ((pipelinesData.value as { items?: PipelineItem[] } | null)?.items ?? []),
)

const dialogOpen = ref(false)
const deleteDialogOpen = ref(false)
const editingId = ref<string | null>(null)
const editingType = ref('')
const saving = ref(false)
const deleting = ref(false)
const formError = ref<string | null>(null)
const deleteTarget = ref<TriggerItem | null>(null)

const defaultForm: TriggerForm = {
  pipeline_id: '',
  trigger_type: '',
  active: true,
  webhook_url: '',
  webhook_method: 'POST',
  webhook_headers: '',
  cron_expression: '',
  cron_timezone: 'UTC',
  input_template: '',
  connector_instance_id: '',
  poll_query: '',
  poll_interval: 60,
  condition_expression: '',
  signal_source_pipeline: '',
  signal_source_node: '',
}

const form = ref<TriggerForm>({ ...defaultForm })

function typeLabel(type: string): string {
  const labels: Record<string, string> = {
    manual: 'Manual',
    webhook: 'Webhook',
    cron: 'Cron',
    polling: 'Polling',
    agent_signal: 'Agent Signal',
  }
  return labels[type] || type
}

function typeBadgeClass(type: string): string {
  const classes: Record<string, string> = {
    manual: 'badge badge-context-blue',
    webhook: 'badge badge-context-purple',
    cron: 'badge badge-context-amber',
    polling: 'badge badge-context-cyan',
    agent_signal: 'badge badge-context-indigo',
  }
  return classes[type] || 'badge badge-context-slate'
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return '\u2014'
  const d = new Date(ts)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function pipelineName(id: string): string {
  const p = pipelines.value.find(p => p.id === id)
  return p ? p.name : shortId(id)
}

function resetForm() {
  form.value = { ...defaultForm }
  formError.value = null
}

function openCreateDialog() {
  editingId.value = null
  editingType.value = ''
  resetForm()
  dialogOpen.value = true
}

function openEditDialog(trigger: TriggerItem) {
  editingId.value = trigger.id
  editingType.value = trigger.trigger_type

  const cfg = trigger.config_json || {}

  form.value = {
    pipeline_id: trigger.pipeline_id,
    trigger_type: trigger.trigger_type,
    active: trigger.active,
    webhook_url: (cfg as any).url || '',
    webhook_method: (cfg as any).method || 'POST',
    webhook_headers: (cfg as any).headers ? JSON.stringify((cfg as any).headers, null, 2) : '',
    cron_expression: trigger.cron_expression || '',
    cron_timezone: trigger.cron_timezone || 'UTC',
    input_template: (cfg as any).input_template ? JSON.stringify((cfg as any).input_template, null, 2) : '',
    connector_instance_id: (cfg as any).connector_instance_id || '',
    poll_query: (cfg as any).poll_query || '',
    poll_interval: (cfg as any).poll_interval_seconds || 60,
    condition_expression: (cfg as any).condition_expression || '',
    signal_source_pipeline: (cfg as any).source_pipeline_id || '',
    signal_source_node: (cfg as any).source_node_id || '',
  }
  dialogOpen.value = true
}

function confirmDelete(trigger: TriggerItem) {
  deleteTarget.value = trigger
  deleteDialogOpen.value = true
}

async function saveTrigger() {
  formError.value = null

  if (!form.value.pipeline_id) {
    formError.value = 'Please select a pipeline.'
    return
  }

  if (!editingId.value && !form.value.trigger_type) {
    formError.value = 'Please select a trigger type.'
    return
  }

  try {
    const triggerType = editingId.value ? editingType.value : form.value.trigger_type
    const configJson: Record<string, unknown> = {}

    if (triggerType === 'webhook') {
      if (form.value.webhook_url) configJson.url = form.value.webhook_url
      if (form.value.webhook_method) configJson.method = form.value.webhook_method
      if (form.value.webhook_headers) {
        try {
          configJson.headers = JSON.parse(form.value.webhook_headers)
        } catch {
          formError.value = 'Headers must be valid JSON.'
          return
        }
      }
    }

    if (triggerType === 'polling') {
      if (form.value.connector_instance_id) configJson.connector_instance_id = form.value.connector_instance_id
      if (form.value.poll_query) configJson.poll_query = form.value.poll_query
      configJson.poll_interval_seconds = form.value.poll_interval
      if (form.value.condition_expression) configJson.condition_expression = form.value.condition_expression
    }

    if (triggerType === 'cron' && form.value.input_template) {
      try {
        configJson.input_template = JSON.parse(form.value.input_template)
      } catch {
        formError.value = 'Input template must be valid JSON.'
        return
      }
    }

    if (triggerType === 'agent_signal') {
      if (form.value.signal_source_pipeline) configJson.source_pipeline_id = form.value.signal_source_pipeline
      if (form.value.signal_source_node) configJson.source_node_id = form.value.signal_source_node
    }

    saving.value = true

    if (editingId.value) {
      const body: Record<string, unknown> = {
        active: form.value.active,
        config_json: Object.keys(configJson).length > 0 ? configJson : undefined,
      }
      if (triggerType === 'cron') {
        if (form.value.cron_expression) body.cron_expression = form.value.cron_expression
        body.cron_timezone = form.value.cron_timezone || 'UTC'
      }
      const { error: err } = await api.PUT('/api/v1/triggers/{trigger_id}', {
        params: { path: { trigger_id: editingId.value } },
        body: body as any,
      })
      if (err) {
        formError.value = `Failed to update trigger: ${formatApiError(err)}`
        return
      }
    } else {
      const body: Record<string, unknown> = {
        trigger_type: triggerType,
        active: form.value.active,
        config_json: configJson,
      }
      if (triggerType === 'cron') {
        if (form.value.cron_expression) body.cron_expression = form.value.cron_expression
        body.cron_timezone = form.value.cron_timezone || 'UTC'
      }
      const { error: err } = await api.POST('/api/v1/pipelines/{pipeline_id}/triggers', {
        params: { path: { pipeline_id: form.value.pipeline_id } },
        body: body as any,
      })
      if (err) {
        formError.value = `Failed to create trigger: ${formatApiError(err)}`
        return
      }
    }

    dialogOpen.value = false
    await loadTriggers()
  } catch (e: unknown) {
    formError.value = `Error: ${formatApiError(e)}`
  } finally {
    saving.value = false
  }
}

async function deleteTrigger() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    const { error: err } = await api.DELETE('/api/v1/triggers/{trigger_id}', {
      params: { path: { trigger_id: deleteTarget.value.id } },
    })
    if (err) {
      error.value = `Failed to delete trigger: ${formatApiError(err)}`
      return
    }
    deleteDialogOpen.value = false
    deleteTarget.value = null
    await loadTriggers()
  } catch (e: unknown) {
    error.value = `Error deleting trigger: ${formatApiError(e)}`
  } finally {
    deleting.value = false
  }
}

const triggerToggling = ref<Record<string, boolean>>({})

async function toggleActive(trigger: TriggerItem) {
  triggerToggling.value[trigger.id] = true
  try {
    const { error: err } = await api.POST('/api/v1/triggers/{trigger_id}/toggle', {
      params: { path: { trigger_id: trigger.id } },
    })
    if (err) {
      error.value = `Failed to toggle trigger: ${formatApiError(err)}`
      return
    }
    await loadTriggers()
  } catch (e: unknown) {
    error.value = `Error toggling trigger: ${formatApiError(e)}`
  } finally {
    triggerToggling.value[trigger.id] = false
  }
}

async function loadAll() {
  await Promise.all([loadTriggers(), loadPipelines()])
}

function triggerActions(trigger: TriggerItem) {
  return [
    {
      key: 'edit',
      label: 'Edit',
      onClick: () => openEditDialog(trigger),
    },
    {
      key: 'delete',
      label: 'Delete',
      onClick: () => confirmDelete(trigger),
      danger: true,
    },
  ]
}

onMounted(() => { planStore.fetchPlan(); loadAll() })

</script>
