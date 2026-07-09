<template>
  <FeatureGate feature-name="plugin_management" required-tier="team" show-disabled>

    <div class="page-narrow">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-semibold tracking-tight">Connectors</h1>
        <p class="mt-1 text-muted-foreground">Manage connector instances for data source integration</p>
      </div>
      <Button
        variant="default"
        class="btn-glow border-primary/30 hover:border-primary/60"
        data-testid="admin-connectors-add"
        @click="openAddForm"
      >
        Add Connector
      </Button>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadConnectors" />

    <template v-else>
      <div v-if="formMode === 'add'" class="card p-6">
        <h2 class="mb-4 text-base font-semibold">New Connector</h2>
        <form @submit.prevent="createConnector">
          <div class="space-y-4">
            <div>
              <label class="mb-1 block text-sm font-medium">Name</label>
              <input
                v-model="formData.name"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="My connector"
                data-testid="admin-connectors-name-input"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Type</label>
              <select
                v-model="formData.connector_type"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                data-testid="admin-connectors-type-select"
                aria-label="Type"
              >
                <option value="postgresql">PostgreSQL</option>
                <option value="mysql">MySQL</option>
                <option value="bigquery">BigQuery</option>
                <option value="snowflake">Snowflake</option>
                <option value="redshift">Redshift</option>
                <option value="http">HTTP API</option>
              </select>
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Description</label>
              <input
                v-model="formData.description"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="Optional description"
                data-testid="admin-connectors-description-input"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Configuration (JSON)</label>
              <textarea
                v-model="formData.config_json"
                rows="6"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                placeholder='{ "host": "localhost", "port": 5432 }'
                data-testid="admin-connectors-config-input"
              ></textarea>
            </div>
            <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
            <div class="flex items-center gap-2">
              <Button
                :disabled="saving || !formData.name.trim()"
                type="submit"
                variant="default"
                data-testid="admin-connectors-submit"
              >
                {{ saving ? 'Creating...' : 'Create' }}
              </Button>
              <button
                type="button"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                data-testid="admin-connectors-cancel"
                @click="closeForm"
              >
                Cancel
              </button>
            </div>
          </div>
        </form>
      </div>

      <div v-if="nativeConnectors.length === 0" class="card p-8 text-center">
        <p class="text-lg font-medium">No connectors configured</p>
        <p class="mt-1 text-sm text-muted-foreground">
          Add a connector to integrate with external data sources.
        </p>
      </div>

      <div v-else class="table-wrapper">
        <table class="w-full text-left text-sm">
          <thead>
            <tr>
              <th class="table-header">Name</th>
              <th class="table-header">Type</th>
              <th class="table-header">Description</th>
              <th class="table-header">Status</th>
              <th class="table-header table-cell-numeric">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="connector in nativeConnectors"
              :key="connector.id"
              class="hover:bg-muted/30 transition-colors"
              :data-testid="`connector-row-${connector.id}`"
            >
              <td class="table-cell font-medium">{{ connector.name }}</td>
              <td class="table-cell">
                <span class="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
                  {{ connector.connector_type }}
                </span>
              </td>
              <td class="table-cell text-muted-foreground">{{ connector.description || '—' }}</td>
              <td class="table-cell">
                <span
                  class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
                  :class="connector.enabled ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                >
                  <span
                    class="h-1.5 w-1.5 rounded-full"
                    :class="connector.enabled ? 'bg-success' : 'bg-muted-foreground'"
                  />
                  {{ connector.enabled ? 'Enabled' : 'Disabled' }}
                </span>
              </td>
              <td class="table-cell-numeric">
                <div class="flex items-center justify-end gap-1">
                  <button
                    class="rounded p-1 text-muted-foreground hover:bg-accent"
                    data-testid="admin-connectors-edit"
                    :aria-label="'Edit connector'"
                    title="Edit connector"
                    @click="openEditForm(connector)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                    </svg>
                  </button>
                  <button
                    class="rounded p-1 text-destructive hover:bg-destructive/10"
                    data-testid="admin-connectors-delete"
                    :aria-label="'Delete connector'"
                    title="Delete connector"
                    @click="confirmDelete(connector)"
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

      <details v-if="previewConnectors.length > 0" class="rounded-lg border bg-card" data-testid="connectors-preview-section">
        <summary class="cursor-pointer px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">
          {{ $t('views.AdminConnectorsView.preview_connectors_count', { count: previewConnectors.length }, previewConnectors.length) }}
        </summary>
        <div class="overflow-hidden border-t">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header">Name</th>
                <th class="table-header">Type</th>
                <th class="table-header">Tier</th>
                <th class="table-header table-cell-numeric">Actions</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr
                v-for="connector in previewConnectors"
                :key="connector.id"
                class="hover:bg-muted/30 transition-colors"
                :data-testid="`connector-row-${connector.id}`"
              >
                <td class="table-cell font-medium">{{ connector.name }}</td>
                <td class="table-cell">
                  <span class="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
                    {{ connector.connector_type }}
                  </span>
                </td>
                <td class="table-cell">
                  <span class="badge badge-context-amber text-xs">{{ $t('views.AdminConnectorsView.preview_badge') }}</span>
                </td>
                <td class="table-cell-numeric">
                  <div class="flex items-center justify-end gap-1">
                    <button
                      class="rounded p-1 text-muted-foreground hover:bg-accent"
                      data-testid="admin-connectors-edit"
                      :aria-label="'Edit connector'"
                      title="Edit connector"
                      @click="openEditForm(connector)"
                    >
                      <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                      </svg>
                    </button>
                    <button
                      class="rounded p-1 text-destructive hover:bg-destructive/10"
                      data-testid="admin-connectors-delete"
                      :aria-label="'Delete connector'"
                      title="Delete connector"
                      @click="confirmDelete(connector)"
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
      </details>

      <div v-if="editConnectorId" class="card p-6">
        <h2 class="mb-4 text-base font-semibold">Edit Connector</h2>
        <form @submit.prevent="updateConnector">
          <div class="space-y-4">
            <div>
              <label class="mb-1 block text-sm font-medium">Name</label>
              <input
                v-model="formData.name"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                data-testid="admin-connectors-edit-name"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Description</label>
              <input
                v-model="formData.description"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                data-testid="admin-connectors-edit-description"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Configuration (JSON)</label>
              <textarea
                v-model="formData.config_json"
                rows="6"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                data-testid="admin-connectors-edit-config"
              ></textarea>
            </div>
            <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
            <div class="flex items-center gap-2">
              <Button
                :disabled="saving || !formData.name.trim()"
                type="submit"
                variant="default"
                data-testid="admin-connectors-save"
              >
                {{ saving ? 'Saving...' : 'Save' }}
              </Button>
              <button
                type="button"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                data-testid="admin-connectors-edit-cancel"
                @click="closeEditForm"
              >
                Cancel
              </button>
            </div>
          </div>
        </form>
      </div>

      <div v-if="deleteConfirmConnectorId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
        <p class="text-sm font-medium text-destructive">Delete "{{ deleteConfirmName }}"?</p>
        <p class="mt-1 text-sm text-destructive/80">This action cannot be undone.</p>
        <div class="mt-3 flex items-center gap-2">
          <Button
            :disabled="deleting"
            variant="destructive"
            data-testid="admin-connectors-delete-confirm"
            @click="deleteConnector"
          >
            {{ deleting ? 'Deleting...' : 'Delete' }}
          </Button>
          <button
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="admin-connectors-delete-cancel"
            @click="deleteConfirmConnectorId = null"
          >
            Cancel
          </button>
        </div>
        <div v-if="deleteError" class="mt-2 text-sm text-destructive">{{ deleteError }}</div>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { Button } from '@/components/ui/button'

const planStore = usePlanStore()

type ConnectorItem = components['schemas']['ConnectorItem'] & {
  enabled?: boolean
  tier?: 'native' | 'preview' | 'in_dev'
}

interface ConnectorFormState {
  name: string
  connector_type: string
  description: string
  config_json: string
}

function emptyForm(): ConnectorFormState {
  return {
    name: '',
    connector_type: 'postgresql',
    description: '',
    config_json: '',
  }
}

const connectors = ref<ConnectorItem[]>([])

// In-dev connectors are hidden entirely; native connectors stay in the
// primary table; preview connectors are segregated into a collapsed
// disclosure section.
const nativeConnectors = computed(() => connectors.value.filter(c => (c.tier ?? 'native') !== 'preview' && (c.tier ?? 'native') !== 'in_dev'))
const previewConnectors = computed(() => connectors.value.filter(c => c.tier === 'preview'))
const loading = ref(true)
const error = ref<string | null>(null)

const formMode = ref<'add' | 'edit' | null>(null)
const formData = reactive<ConnectorFormState>(emptyForm())
const editConnectorId = ref<string | null>(null)

const saving = ref(false)
const formError = ref<string | null>(null)

const deleteConfirmConnectorId = ref<string | null>(null)
const deleteConfirmName = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)

async function loadConnectors() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/connectors')
    if (err) {
      error.value = `Failed to load connectors: ${formatApiError(err)}`
    } else if (data) {
      connectors.value = data.items
    }
  } catch (e: unknown) {
    error.value = `Failed to load connectors: ${formatApiError(e)}`
  } finally {
    loading.value = false
  }
}

function openAddForm() {
  formMode.value = 'add'
  Object.assign(formData, emptyForm())
  editConnectorId.value = null
  deleteConfirmConnectorId.value = null
  formError.value = null
}

function openEditForm(connector: ConnectorItem) {
  formMode.value = 'edit'
  editConnectorId.value = connector.id
  deleteConfirmConnectorId.value = null
  formError.value = null
  Object.assign(formData, {
    name: connector.name,
    connector_type: connector.connector_type,
    description: connector.description ?? '',
    config_json: '',
  })
}

function closeForm() {
  formMode.value = null
  Object.assign(formData, emptyForm())
  formError.value = null
}

function closeEditForm() {
  editConnectorId.value = null
  Object.assign(formData, emptyForm())
  formError.value = null
}

function buildCreateBody() {
  return {
    name: formData.name.trim(),
    connector_type: formData.connector_type,
    description: formData.description.trim() || null,
    config_json: formData.config_json.trim() || null,
  }
}

function buildUpdateBody() {
  return {
    name: formData.name.trim() || null,
    description: formData.description.trim() || null,
    config_json: formData.config_json.trim() || null,
  }
}

async function createConnector() {
  if (!formData.name.trim()) return
  saving.value = true
  formError.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/connectors', {
      body: buildCreateBody(),
    })
    if (err) {
      formError.value = formatApiError(err)
    } else if (data) {
      connectors.value.push({
        id: data.id,
        name: data.name,
        connector_type: data.connector_type,
        description: data.description,
      })
      closeForm()
    }
  } catch (e: unknown) {
    formError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

async function updateConnector() {
  if (!editConnectorId.value || !formData.name.trim()) return
  saving.value = true
  formError.value = null
  try {
    const { data, error: err } = await api.PUT('/api/v1/connectors/{connector_id}', {
      params: { path: { connector_id: editConnectorId.value } },
      body: buildUpdateBody(),
    })
    if (err) {
      formError.value = formatApiError(err)
    } else if (data) {
      const idx = connectors.value.findIndex(c => c.id === editConnectorId.value)
      if (idx >= 0) {
        connectors.value[idx] = {
          id: data.id,
          name: data.name,
          connector_type: data.connector_type,
          description: data.description,
        }
      }
      closeEditForm()
    }
  } catch (e: unknown) {
    formError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

function confirmDelete(connector: ConnectorItem) {
  deleteConfirmConnectorId.value = connector.id
  deleteConfirmName.value = connector.name
  editConnectorId.value = null
  deleteError.value = null
}

async function deleteConnector() {
  if (!deleteConfirmConnectorId.value) return
  deleting.value = true
  deleteError.value = null
  try {
    const { error: err, response } = await api.DELETE('/api/v1/connectors/{connector_id}', {
      params: { path: { connector_id: deleteConfirmConnectorId.value } },
    })
    if (err) {
      deleteError.value = formatApiError(err)
    } else if (response.status === 204 || response.ok) {
      connectors.value = connectors.value.filter(c => c.id !== deleteConfirmConnectorId.value)
      deleteConfirmConnectorId.value = null
    }
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  } finally {
    deleting.value = false
  }
}

onMounted(() => { planStore.fetchPlan(); loadConnectors() })
</script>

