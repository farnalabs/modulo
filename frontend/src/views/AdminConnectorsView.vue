<template>
  <FeatureGate feature-name="plugin_management" required-tier="community" show-disabled>

    <div class="page-wide">
    <header class="flex items-center justify-between">
      <PageHeader :title="$t('views.AdminConnectorsView.title')" :subtitle="$t('views.AdminConnectorsView.subtitle')" />
      <Button class="border-primary/30 hover:border-primary/60" data-testid="admin-connectors-add" @click="openAddForm">
        {{ $t('views.AdminConnectorsView.add_connector') }}
      </Button>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadConnectors" />

    <template v-else>
      <div v-if="formMode === 'add'" class="card p-6">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.AdminConnectorsView.new_connector') }}</h2>
        <form @submit.prevent="createConnector">
          <div class="space-y-4">
            <div>
              <label for="adminconnectorsview-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.AdminConnectorsView.name') }}</label>
              <input id="adminconnectorsview-field-7"
                v-model="formData.name"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                :placeholder="$t('views.AdminConnectorsView.name_placeholder')"
                data-testid="admin-connectors-name-input"
              />
            </div>
            <div>
              <label for="adminconnectorsview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.AdminConnectorsView.type') }}</label>
              <Select
  :aria-label="$t('views.AdminConnectorsView.type')"
  v-model="formData.connector_type"
  :placeholder="$t('views.AdminConnectorsView.type_placeholder')"
  data-testid="admin-connectors-type-select"
  class="w-full"
  :options="connectorTypes.map(ct => ({ value: ct.id, label: ct.display_name }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
            </div>
            <div>
              <label for="adminconnectorsview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.AdminConnectorsView.description') }}</label>
              <input id="adminconnectorsview-field-5"
                v-model="formData.description"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                :placeholder="$t('views.AdminConnectorsView.description_placeholder')"
                data-testid="admin-connectors-description-input"
              />
            </div>
            <div v-if="isRestConnector">
              <RestConnectorConfigForm
                ref="restFormRef"
                v-model:config="restConfig"
                v-model:credentials="restCreds"
                v-model:credsDirty="credsDirty"
                v-model:credsIdentityDirty="credsIdentityDirty"
                :mode="formMode"
              />
            </div>
            <div v-else>
              <label for="adminconnectorsview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.AdminConnectorsView.configuration_json') }}</label>
              <textarea id="adminconnectorsview-field-4"
                v-model="formData.config_json"
                rows="6"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                placeholder='{ "host": "localhost", "port": 5432 }'
                data-testid="admin-connectors-config-input"
              ></textarea>
            </div>
            <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
            <div class="flex items-center gap-2">
              <Button :disabled="saving || !formData.name.trim()" type="submit" data-testid="admin-connectors-submit">
                {{ saving ? $t('views.AdminConnectorsView.creating') : $t('views.AdminConnectorsView.create') }}
              </Button>
              <button
                type="button"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                data-testid="admin-connectors-cancel"
                @click="closeForm"
              >
                {{ $t('views.AdminConnectorsView.cancel') }}
              </button>
            </div>
          </div>
        </form>
      </div>

      <div v-if="nativeConnectors.length === 0" class="card p-8 text-center">
        <p class="text-lg font-medium">{{ $t('views.AdminConnectorsView.no_connectors_configured') }}</p>
        <p class="mt-1 text-sm text-muted-foreground">
          {{ $t('views.AdminConnectorsView.add_a_connector_to_integrate') }}
        </p>
      </div>

      <div v-else class="table-wrapper overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead>
            <tr>
              <th class="table-header">{{ $t('views.AdminConnectorsView.name') }}</th>
              <th class="table-header">{{ $t('views.AdminConnectorsView.type') }}</th>
              <th class="table-header">{{ $t('views.AdminConnectorsView.description') }}</th>
              <th class="table-header capitalize">{{ $t('views.AdminConnectorsView.status') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.AdminConnectorsView.actions') }}</th>
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
                  {{ connector.enabled ? $t('views.AdminConnectorsView.enabled') : $t('views.AdminConnectorsView.disabled') }}
                </span>
              </td>
              <td class="table-cell-numeric">
                <TableActions :actions="connectorActions(connector)" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <details v-if="previewConnectors.length > 0" class="rounded-lg border bg-card" data-testid="connectors-preview-section">
        <summary class="cursor-pointer px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">
          {{ $t('views.AdminConnectorsView.preview_connectors_count', { count: previewConnectors.length }, previewConnectors.length) }}
        </summary>
        <div class="overflow-x-auto border-t">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header">{{ $t('views.AdminConnectorsView.name') }}</th>
                <th class="table-header">{{ $t('views.AdminConnectorsView.type') }}</th>
                <th class="table-header">{{ $t('views.AdminConnectorsView.tier') }}</th>
                <th class="table-header table-cell-numeric">{{ $t('views.AdminConnectorsView.actions') }}</th>
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
                  <TableActions :actions="connectorActions(connector)" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>

      <div v-if="editConnectorId" class="card p-6">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.AdminConnectorsView.edit_connector') }}</h2>
        <form @submit.prevent="updateConnector">
          <div class="space-y-4">
            <div>
              <label for="adminconnectorsview-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.AdminConnectorsView.name') }}</label>
              <input id="adminconnectorsview-field-3"
                v-model="formData.name"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                data-testid="admin-connectors-edit-name"
              />
            </div>
            <div>
              <label for="adminconnectorsview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.AdminConnectorsView.description') }}</label>
              <input id="adminconnectorsview-field-2"
                v-model="formData.description"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                data-testid="admin-connectors-edit-description"
              />
            </div>
            <div v-if="isRestConnector">
              <RestConnectorConfigForm
                ref="restFormRef"
                v-model:config="restConfig"
                v-model:credentials="restCreds"
                v-model:credsDirty="credsDirty"
                v-model:credsIdentityDirty="credsIdentityDirty"
                :mode="formMode"
              />
            </div>
            <div v-else>
              <label for="adminconnectorsview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.AdminConnectorsView.configuration_json') }}</label>
              <textarea id="adminconnectorsview-field-1"
                v-model="formData.config_json"
                rows="6"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                data-testid="admin-connectors-edit-config"
              ></textarea>
            </div>
            <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
            <div class="flex items-center gap-2">
              <Button :disabled="saving || !formData.name.trim()" type="submit" data-testid="admin-connectors-save">
                {{ saving ? $t('views.AdminConnectorsView.saving') : $t('views.AdminConnectorsView.save') }}
              </Button>
              <button
                type="button"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                data-testid="admin-connectors-edit-cancel"
                @click="closeEditForm"
              >
                {{ $t('views.AdminConnectorsView.cancel') }}
              </button>
            </div>
          </div>
        </form>
      </div>

      <div v-if="deleteConfirmConnectorId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
        <p class="text-sm font-medium text-destructive">{{ $t('views.AdminConnectorsView.delete_confirm', { name: deleteConfirmName }) }}</p>
        <p class="mt-1 text-sm text-destructive/80">{{ $t('views.AdminConnectorsView.this_action_cannot_be_undone') }}</p>
        <div class="mt-3 flex items-center gap-2">
          <Button :disabled="deleting" severity="danger" data-testid="admin-connectors-delete-confirm" @click="deleteConnector">
            {{ deleting ? $t('views.AdminConnectorsView.deleting') : $t('views.AdminConnectorsView.delete') }}
          </Button>
          <button type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="admin-connectors-delete-cancel"
            @click="deleteConfirmConnectorId = null"
          >
            {{ $t('views.AdminConnectorsView.cancel') }}
          </button>
        </div>
        <div v-if="deleteError" class="mt-2 text-sm text-destructive">{{ deleteError }}</div>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import Button from 'primevue/button'
import Select from 'primevue/select'
import TableActions from '../components/shared/TableActions.vue'
import RestConnectorConfigForm from '../components/connectors/RestConnectorConfigForm.vue'
import { REST_FLAT_FIELDS, type RestConfigState, type RestCredsState } from '../components/connectors/RestConnectorConfigForm.vue'

const { t } = useI18n()
const planStore = usePlanStore()

interface ConnectorItem {
  id: string
  name: string
  connector_type: string
  description?: string | null
  enabled?: boolean
  tier?: 'native' | 'preview' | 'in_dev'
  config_json?: Record<string, unknown> | null
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
    connector_type: '',
    description: '',
    config_json: '',
  }
}

// REST connector (Generic REST) structured form state. Operational config is
// kept separate from credentials (auth) so they are never conflated on submit.
// The flat + advanced field lists come from the form component so they stay the
// single source of truth (REST_FLAT_FIELDS / REST_ADVANCED_FIELDS).
const AUTH_IDENTITY_FIELDS = ['auth_mode', 'in', 'header_name', 'query_param_name']
const NON_ADVANCED_FIELDS = new Set<string>([...REST_FLAT_FIELDS, ...AUTH_IDENTITY_FIELDS])

const REST_ON_UNKNOWN_OPTIONS = ['fail_open', 'fail_closed', 'off']
const REST_AUTH_MODE_OPTIONS = ['bearer', 'api_key', 'basic']

function defaultRestConfig(): RestConfigState {
  return {
    base_url: '',
    method: 'GET',
    timeout_seconds: 30,
    verify_tls: true,
    on_unknown: 'fail_open',
    records_path: '',
    allowed_hosts: '',
    advanced_json: '',
  }
}

function defaultRestCreds(): RestCredsState {
  return {
    auth_mode: 'bearer',
    token: '',
    username: '',
    password: '',
    api_key: '',
    apiKeyIn: 'header',
    header_name: '',
    query_param_name: '',
  }
}

const restConfig = ref<RestConfigState>(defaultRestConfig())
const restCreds = ref<RestCredsState>(defaultRestCreds())
const credsDirty = ref(false)
// Tracks NON-SECRET auth identity edits (auth_mode / apiKeyIn / header_name /
// query_param_name) so an identity-only change re-sends the credentials payload
// (the backend overlays it while preserving the stored secret, FAR-466).
const credsIdentityDirty = ref(false)
const restFormRef = ref<InstanceType<typeof RestConnectorConfigForm> | null>(null)
const isRestConnector = computed(() => formData.connector_type === 'rest')

function resetRestForm() {
  Object.assign(restConfig.value, defaultRestConfig())
  Object.assign(restCreds.value, defaultRestCreds())
  credsDirty.value = false
  credsIdentityDirty.value = false
}

function prefillRestConfig(connector: ConnectorItem) {
  const cfg = connector.config_json ?? {}
  restConfig.value.base_url = typeof cfg.base_url === 'string' ? cfg.base_url : ''
  restConfig.value.method = typeof cfg.method === 'string' ? cfg.method.toUpperCase() : 'GET'
  restConfig.value.timeout_seconds = typeof cfg.timeout_seconds === 'number' ? cfg.timeout_seconds : 30
  restConfig.value.verify_tls = typeof cfg.verify_tls === 'boolean' ? cfg.verify_tls : true
  restConfig.value.on_unknown = REST_ON_UNKNOWN_OPTIONS.includes(String(cfg.on_unknown)) ? String(cfg.on_unknown) : 'fail_open'
  restConfig.value.records_path = typeof cfg.records_path === 'string' ? cfg.records_path : ''
  restConfig.value.allowed_hosts = Array.isArray(cfg.allowed_hosts)
    ? (cfg.allowed_hosts as unknown[]).join(', ')
    : typeof cfg.allowed_hosts === 'string'
      ? cfg.allowed_hosts
      : ''
  const advanced: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(cfg)) {
    // Snapshot any stored config key the form does not surface as a first-class
    // control back into the JSON editor: known advanced_fields AND genuinely
    // unknown keys. This preserves them on an edit-save instead of silently
    // dropping the config (no data loss on edit, FAR-466).
    if (NON_ADVANCED_FIELDS.has(key)) continue
    advanced[key] = value
  }
  restConfig.value.advanced_json = Object.keys(advanced).length ? JSON.stringify(advanced, null, 2) : ''
  // Credentials are write-only in the API (has_credentials boolean only) — the
  // secret VALUES can never be read back. But the NON-SECRET auth identity
  // (auth_mode, api_key in/header_name, query_param_name) is echoed back from
  // config_json so edit mode displays the real auth profile instead of
  // silently resetting to defaults. Only the secret values stay write-only.
  const creds = defaultRestCreds()
  const storedMode = String(cfg.auth_mode || '').toLowerCase()
  if (REST_AUTH_MODE_OPTIONS.includes(storedMode)) creds.auth_mode = storedMode
  if (String(cfg.in) === 'query') creds.apiKeyIn = 'query'
  if (typeof cfg.header_name === 'string') creds.header_name = cfg.header_name
  if (typeof cfg.query_param_name === 'string') creds.query_param_name = cfg.query_param_name
  Object.assign(restCreds.value, creds)
  credsDirty.value = false
  credsIdentityDirty.value = false
}

function buildRestConfig(): Record<string, unknown> {
  const cfg: Record<string, unknown> = {
    description: formData.description.trim(),
    base_url: restConfig.value.base_url.trim(),
    method: String(restConfig.value.method).toUpperCase(),
    timeout_seconds: Number(restConfig.value.timeout_seconds) || 30,
    verify_tls: !!restConfig.value.verify_tls,
    on_unknown: restConfig.value.on_unknown,
    records_path: restConfig.value.records_path.trim(),
  }
  if (restConfig.value.allowed_hosts.trim()) {
    cfg.allowed_hosts = restConfig.value.allowed_hosts.split(',').map(s => s.trim()).filter(Boolean)
  }
  if (restConfig.value.advanced_json.trim()) {
    try {
      Object.assign(cfg, JSON.parse(restConfig.value.advanced_json) as Record<string, unknown>)
    } catch {
      // validated in the form component; never reached here
    }
  }
  // Echo the NON-SECRET auth identity into config_json so a subsequent edit can
  // prefill it. The secret VALUES are protected — they live only in the
  // credentials payload, never here. The connector reads auth from the
  // credentials (creds) payload, so these mirrored keys are purely for the
  // edit-mode echo and never leak a secret.
  cfg.auth_mode = restCreds.value.auth_mode
  if (restCreds.value.auth_mode === 'api_key') {
    cfg.in = restCreds.value.apiKeyIn
    if (restCreds.value.apiKeyIn === 'header') cfg.header_name = restCreds.value.header_name
    else cfg.query_param_name = restCreds.value.query_param_name
  }
  return cfg
}

function buildRestCredentials(): string {
  const mode = restCreds.value.auth_mode
  const creds: Record<string, unknown> = { auth_mode: mode }
  if (mode === 'bearer') creds.token = restCreds.value.token
  if (mode === 'basic') {
    creds.username = restCreds.value.username
    creds.password = restCreds.value.password
  }
  if (mode === 'api_key') {
    creds.api_key = restCreds.value.api_key
    creds.in = restCreds.value.apiKeyIn
    if (restCreds.value.apiKeyIn === 'header') creds.header_name = restCreds.value.header_name
    else creds.query_param_name = restCreds.value.query_param_name
  }
  return JSON.stringify(creds)
}

const { loading, error, data, load: loadConnectors } = useDataFetch(
  () => api.GET('/api/v1/connectors'),
  { immediate: false },
)

const connectors = ref<ConnectorItem[]>([])
const connectorTypes = ref<{id: string, display_name: string}[]>([])

async function loadConnectorTypes() {
  const resp = await api.GET('/api/v1/connectors/types')
  if (resp.data?.items) {
    connectorTypes.value = resp.data.items as {id: string, display_name: string}[]
  }

}

watch(data, response => {
  const items = (response as { items?: components['schemas']['ConnectorResponse'][] } | null)?.items ?? []
  connectors.value = items.map(item => ({
    id: item.id,
    name: item.name,
    connector_type: item.connector_type_id,
    description: typeof item.config_json?.description === 'string' ? item.config_json.description : null,
    enabled: item.status === 'active',
    tier: item.tier === 'preview' || item.tier === 'in_dev' ? item.tier : 'native',
    config_json: item.config_json ?? null,
  }))
}, { immediate: true })
const nativeConnectors = computed(() => connectors.value.filter(c => (c.tier ?? 'native') !== 'preview' && (c.tier ?? 'native') !== 'in_dev'))
const previewConnectors = computed(() => connectors.value.filter(c => c.tier === 'preview'))

const formMode = ref<'add' | 'edit' | null>(null)
const formData = reactive<ConnectorFormState>(emptyForm())
const editConnectorId = ref<string | null>(null)

const saving = ref(false)
const formError = ref<string | null>(null)

const deleteConfirmConnectorId = ref<string | null>(null)
const deleteConfirmName = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)

function openAddForm() {
  formMode.value = 'add'
  Object.assign(formData, emptyForm())
  editConnectorId.value = null
  deleteConfirmConnectorId.value = null
  formError.value = null
  resetRestForm()
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
  if (connector.connector_type === 'rest') {
    prefillRestConfig(connector)
  } else {
    resetRestForm()
  }
}

function closeForm() {
  formMode.value = null
  Object.assign(formData, emptyForm())
  formError.value = null
  resetRestForm()
}

function closeEditForm() {
  editConnectorId.value = null
  Object.assign(formData, emptyForm())
  formError.value = null
  resetRestForm()
}

function buildCreateBody() {
  if (isRestConnector.value) {
    return {
      name: formData.name.trim(),
      connector_type_id: formData.connector_type,
      credentials: buildRestCredentials(),
      config_json: buildRestConfig(),
      allowed_operations: [],
      visibility: 'org',
      tier: 'native' as const,
    }
  }
  return {
    name: formData.name.trim(),
    connector_type_id: formData.connector_type,
    credentials: formData.config_json,
    config_json: { description: formData.description.trim() },
    allowed_operations: [],
    visibility: 'org',
    tier: 'native' as const,
  }
}

function buildUpdateBody() {
  if (isRestConnector.value) {
    const body: Record<string, unknown> = {
      name: formData.name.trim() || null,
      config_json: buildRestConfig(),
    }
    // Credentials are write-only; only re-send (full replace) when the user
    // actually edited the auth section, otherwise the existing encrypted
    // credential is preserved. An identity-only edit (credsIdentityDirty) still
    // re-sends the credentials so the backend overlays the new identity while
    // preserving the stored secret (FAR-466); credsDirty alone was insufficient
    // because the connector reads auth identity from the credentials payload,
    // not config_json.
    if (credsDirty.value || credsIdentityDirty.value) body.credentials = buildRestCredentials()
    return body
  }
  return {
    name: formData.name.trim() || null,
    credentials: formData.config_json.trim() || null,
    config_json: { description: formData.description.trim() },
  }
}

async function createConnector() {
  if (!formData.name.trim()) return
  if (isRestConnector.value) {
    const valid = restFormRef.value ? restFormRef.value.validate() : true
    if (!valid) {
      formError.value = t('views.AdminConnectorsView.please_fix')
      return
    }
  }
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
        connector_type: data.connector_type_id,
        description: typeof data.config_json.description === 'string' ? data.config_json.description : null,
        enabled: true,
        config_json: data.config_json ?? null,
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
  if (isRestConnector.value) {
    const valid = restFormRef.value ? restFormRef.value.validate() : true
    if (!valid) {
      formError.value = t('views.AdminConnectorsView.please_fix')
      return
    }
  }
  saving.value = true
  formError.value = null
  try {
    const { data, error: err } = await api.PATCH('/api/v1/connectors/{connector_id}', {
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
          connector_type: data.connector_type_id,
          description: typeof data.config_json.description === 'string' ? data.config_json.description : null,
          enabled: true,
          config_json: data.config_json ?? null,
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

function connectorActions(connector: ConnectorItem) {
  return [
    {
      key: 'edit',
      label: t('views.AdminConnectorsView.edit'),
      onClick: () => openEditForm(connector),
    },
    {
      key: 'delete',
      label: t('views.AdminConnectorsView.delete'),
      onClick: () => confirmDelete(connector),
      danger: true,
    },
  ]
}

onMounted(() => { planStore.fetchPlan(); loadConnectors(); loadConnectorTypes() })
</script>
