<template>
  <FeatureGate feature-name="webhook_trigger" required-tier="team">
    <template #locked="{ tooltip }">
      <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
        <div class="mb-4 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/5 p-4 text-sm text-warning">
          <LockIcon :locked="true" :tooltip="tooltip" />
          <span>Webhook triggers are not available on your current plan.</span>
        </div>
      </div>
    </template>

    <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">Triggers</h1>
        <p class="mt-1 text-muted-foreground">Configure triggers that automatically kick off pipeline runs</p>
      </div>
      <button
        data-testid="settings-triggers-create"
        class="btn-glow rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:border-primary/60 hover:brightness-110 transition-all duration-150"
        @click="openCreateDialog"
      >
        Create Trigger
      </button>
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
                {{ formatTimestamp(t.last_fired_at) }}
              </td>
              <td class="px-4 py-3 text-muted-foreground">
                {{ formatTimestamp(t.next_fire_at) }}
              </td>
              <td class="px-4 py-3 text-right">
                <div class="flex items-center justify-end gap-1">
                  <button
                    class="rounded p-1 text-muted-foreground hover:bg-accent"
                    data-testid="settings-triggers-edit"
                    :aria-label="'Edit trigger'"
                    title="Edit trigger"
                    @click="openEditDialog(t)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                    </svg>
                  </button>
                  <button
                    class="rounded p-1 text-destructive hover:bg-destructive/10"
                    data-testid="settings-triggers-delete"
                    :aria-label="'Delete trigger'"
                    title="Delete trigger"
                    @click="confirmDelete(t)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
                    </svg>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- Create / Edit Dialog -->
    <Dialog :open="dialogOpen" @update:open="dialogOpen = false">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{{ editingId ? 'Edit Trigger' : 'Create Trigger' }}</DialogTitle>
          <DialogDescription>
            {{ editingId ? 'Update the trigger configuration.' : 'Configure a new trigger for your pipeline.' }}
          </DialogDescription>
        </DialogHeader>

        <form @submit.prevent="saveTrigger" class="space-y-4">
          <div>
            <label class="mb-1 block text-sm font-medium">Pipeline</label>
            <select
              v-model="form.pipeline_id"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="settings-triggers-form-pipeline"
              required
            >
              <option value="" disabled>Select pipeline</option>
              <option v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
          </div>

          <div v-if="!editingId">
            <label class="mb-1 block text-sm font-medium">Trigger Type</label>
            <select
              v-model="form.trigger_type"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="settings-triggers-form-type"
              required
            >
              <option value="" disabled>Select type</option>
              <option value="webhook">Webhook</option>
              <option value="cron">Cron</option>
              <option value="polling">Polling</option>
              <option value="agent_signal">Agent Signal</option>
            </select>
          </div>
          <div v-else class="text-sm text-muted-foreground">
            Type: <span class="font-medium">{{ typeLabel(editingType) }}</span>
          </div>

          <!-- Webhook config -->
          <template v-if="form.trigger_type === 'webhook'">
            <div>
              <label class="mb-1 block text-sm font-medium">URL</label>
              <input
                v-model="form.webhook_url"
                type="url"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="https://example.com/webhook"
                data-testid="settings-triggers-form-webhook-url"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">HTTP Method</label>
              <select
                v-model="form.webhook_method"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                data-testid="settings-triggers-form-webhook-method"
              >
                <option value="POST">POST</option>
                <option value="GET">GET</option>
                <option value="PUT">PUT</option>
              </select>
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Headers (JSON)</label>
              <textarea
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
              <label class="mb-1 block text-sm font-medium">Cron Expression</label>
              <input
                v-model="form.cron_expression"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="*/5 * * * *"
                data-testid="settings-triggers-form-cron-expr"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Timezone</label>
              <input
                v-model="form.cron_timezone"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="UTC"
                data-testid="settings-triggers-form-cron-tz"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Input Template (JSON)</label>
              <textarea
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
              <label class="mb-1 block text-sm font-medium">Connector Instance ID</label>
              <input
                v-model="form.connector_instance_id"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="uuid"
                data-testid="settings-triggers-form-polling-connector"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Query</label>
              <textarea
                v-model="form.poll_query"
                rows="3"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                placeholder="SELECT * FROM table WHERE status = 'pending'"
                data-testid="settings-triggers-form-polling-query"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Poll Interval (seconds)</label>
              <input
                v-model="form.poll_interval"
                type="number"
                min="10"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="60"
                data-testid="settings-triggers-form-polling-interval"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Condition Expression (JMESPath)</label>
              <input
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
              <label class="mb-1 block text-sm font-medium">Source Pipeline</label>
              <select
                v-model="form.signal_source_pipeline"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                data-testid="settings-triggers-form-signal-pipeline"
              >
                <option value="" disabled>Select source pipeline</option>
                <option v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</option>
              </select>
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Source Node ID</label>
              <input
                v-model="form.signal_source_node"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="node_abc123"
                data-testid="settings-triggers-form-signal-node"
              />
            </div>
          </template>

          <div class="flex items-center gap-2">
            <label class="flex items-center gap-2 text-sm">
              <input
                v-model="form.active"
                type="checkbox"
                class="rounded border-input"
                data-testid="settings-triggers-form-active"
              />
              Active
            </label>
          </div>

          <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
          <DialogFooter>
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              data-testid="settings-triggers-form-cancel"
              @click="dialogOpen = false"
            >
              Cancel
            </button>
            <button
              :disabled="saving"
              type="submit"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
              data-testid="settings-triggers-form-submit"
            >
              {{ saving ? 'Saving...' : (editingId ? 'Update' : 'Create') }}
            </button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>

    <!-- Delete confirmation dialog -->
    <Dialog :open="deleteDialogOpen" @update:open="deleteDialogOpen = false">
      <DialogContent class="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Delete Trigger</DialogTitle>
          <DialogDescription>
            Are you sure you want to delete this trigger? This action cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="settings-triggers-delete-cancel"
            @click="deleteDialogOpen = false"
          >
            Cancel
          </button>
          <button
            :disabled="deleting"
            type="button"
            class="rounded-lg bg-destructive px-4 py-2 text-sm font-semibold text-destructive-foreground hover:brightness-110 disabled:opacity-50 transition-all"
            data-testid="settings-triggers-delete-confirm"
            @click="deleteTrigger"
          >
            {{ deleting ? 'Deleting...' : 'Delete' }}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../components/ui/dialog'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LockIcon from '../components/LockIcon.vue'

const planStore = usePlanStore()

type TriggerItem = components['schemas']['TriggerItem']
type PipelineItem = components['schemas']['PipelineItem']

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

const items = ref<TriggerItem[]>([])
const pipelines = ref<PipelineItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

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
  return p ? p.name : id.slice(0, 8) + '...'
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

  saving.value = true
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
        formError.value = `Failed to update trigger: ${err}`
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
        formError.value = `Failed to create trigger: ${err}`
        return
      }
    }

    dialogOpen.value = false
    await loadTriggers()
  } catch (e: unknown) {
    formError.value = `Error: ${e instanceof Error ? e.message : String(e)}`
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
      error.value = `Failed to delete trigger: ${err}`
      return
    }
    deleteDialogOpen.value = false
    deleteTarget.value = null
    await loadTriggers()
  } catch (e: unknown) {
    error.value = `Error deleting trigger: ${e instanceof Error ? e.message : String(e)}`
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
      error.value = `Failed to toggle trigger: ${err}`
      return
    }
    await loadTriggers()
  } catch (e: unknown) {
    error.value = `Error toggling trigger: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    triggerToggling.value[trigger.id] = false
  }
}

async function loadTriggers() {
  try {
    const { data, error: err } = await api.GET('/api/v1/triggers', {
      params: { query: { page: 1, page_size: 100 } },
    })
    if (err) {
      error.value = `Failed to load triggers: ${err}`
    } else if (data) {
      items.value = data.items
    }
  } catch (e: unknown) {
    error.value = `Failed to load triggers: ${e instanceof Error ? e.message : String(e)}`
  }
}

async function loadPipelines() {
  try {
    const { data, error: err } = await api.GET('/api/v1/pipelines', {})
    if (!err && data) {
      pipelines.value = data.items
    }
  } catch {
    // Non-fatal — pipeline names will fall back to truncated IDs
  }
}

async function loadAll() {
  loading.value = true
  error.value = null
  await Promise.all([loadTriggers(), loadPipelines()])
  loading.value = false
}

onMounted(() => { planStore.fetchPlan(); loadAll() })
</script>
