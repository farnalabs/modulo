<template>
  <FeatureGate feature-name="plugin_management" required-tier="team" show-disabled>

    <div class="mx-auto max-w-4xl space-y-8 p-6">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">Connectors</h1>
        <p class="mt-1 text-muted-foreground">Manage connector instances for data source integration</p>
      </div>
      <button
        class="btn-glow rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:border-primary/60 hover:brightness-110 transition-all duration-150"
        data-testid="admin-connectors-add"
        @click="openAddForm"
      >
        Add Connector
      </button>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadConnectors" />

    <template v-else>
      <div v-if="formMode === 'add'" class="card p-6">
        <h2 class="mb-4 text-lg font-semibold">New Connector</h2>
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
              <button
                :disabled="saving || !formData.name.trim()"
                type="submit"
                class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
                data-testid="admin-connectors-submit"
              >
                {{ saving ? 'Creating...' : 'Create' }}
              </button>
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

      <div v-if="connectors.length === 0" class="card p-8 text-center">
        <p class="text-lg font-medium">No connectors configured</p>
        <p class="mt-1 text-sm text-muted-foreground">
          Add a connector to integrate with external data sources.
        </p>
      </div>

      <div class="overflow-hidden rounded-lg border">
        <table class="w-full text-left text-sm">
          <thead class="bg-muted/50">
            <tr>
              <th class="px-4 py-3 font-medium">Name</th>
              <th class="px-4 py-3 font-medium">Type</th>
              <th class="px-4 py-3 font-medium">Description</th>
              <th class="px-4 py-3 font-medium">Status</th>
              <th class="px-4 py-3 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="connector in connectors"
              :key="connector.id"
              class="hover:bg-muted/30 transition-colors"
            >
              <td class="px-4 py-3 font-medium">{{ connector.name }}</td>
              <td class="px-4 py-3">
                <span class="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
                  {{ connector.connector_type }}
                </span>
              </td>
              <td class="px-4 py-3 text-muted-foreground">{{ connector.description || '—' }}</td>
              <td class="px-4 py-3">
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
              <td class="px-4 py-3 text-right">
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

      <div v-if="editConnectorId" class="card p-6">
        <h2 class="mb-4 text-lg font-semibold">Edit Connector</h2>
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
              <button
                :disabled="saving || !formData.name.trim()"
                type="submit"
                class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
                data-testid="admin-connectors-save"
              >
                {{ saving ? 'Saving...' : 'Save' }}
              </button>
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
          <button
            :disabled="deleting"
            class="rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:brightness-110 disabled:opacity-50 transition-all"
            data-testid="admin-connectors-delete-confirm"
            @click="deleteConnector"
          >
            {{ deleting ? 'Deleting...' : 'Delete' }}
          </button>
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
import { ref, reactive, onMounted } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'

const planStore = usePlanStore()

type ConnectorItem = components['schemas']['ConnectorItem'] & { enabled?: boolean }

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
      error.value = `Failed to load connectors: ${err}`
    } else if (data) {
      connectors.value = data.items
    }
  } catch (e: unknown) {
    error.value = `Failed to load connectors: ${e instanceof Error ? e.message : String(e)}`
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
      formError.value = String(err)
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
    formError.value = e instanceof Error ? e.message : String(e)
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
      formError.value = String(err)
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
    formError.value = e instanceof Error ? e.message : String(e)
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
      deleteError.value = String(err)
    } else if (response.status === 204 || response.ok) {
      connectors.value = connectors.value.filter(c => c.id !== deleteConfirmConnectorId.value)
      deleteConfirmConnectorId.value = null
    }
  } catch (e: unknown) {
    deleteError.value = e instanceof Error ? e.message : String(e)
  } finally {
    deleting.value = false
  }
}

onMounted(() => { planStore.fetchPlan(); loadConnectors() })
</script>
