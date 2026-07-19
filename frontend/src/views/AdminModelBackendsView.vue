<template>
  <FeatureGate feature-name="model_backend_management" show-disabled>
    <div class="page-narrow">
      <header class="flex items-center justify-between">
        <PageHeader :title="$t('views.AdminModelBackendsView.model_backends')" :subtitle="$t('views.AdminModelBackendsView.manage_llm_backend_connections_and_credentials')" />
        <Button
          variant="default"
           class="border-primary/30 hover:border-primary/60"
          data-testid="admin-model-backends-add"
          @click="openAddForm"
        >
          {{ $t('views.AdminModelBackendsView.add_model_backend') }}
        </Button>
      </header>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadBackends" />

      <template v-else>
        <div v-if="formMode === 'add'" class="card p-6">
          <h2 class="mb-4 text-base font-semibold">{{ $t('views.AdminModelBackendsView.new_model_backend') }}</h2>
          <form @submit.prevent="createBackend">
            <div class="space-y-4">
              <div>
                <label for="adminmodelbackendsview-field-14" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.name') }}</label>
                <input id="adminmodelbackendsview-field-14"
                  v-model="formData.name"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminModelBackendsView.name_placeholder')"
                  data-testid="admin-model-backends-name-input"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-13" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.display_name') }}</label>
                <input id="adminmodelbackendsview-field-13"
                  v-model="formData.display_name"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminModelBackendsView.my_llm_backend')"
                  data-testid="admin-model-backends-display-name-input"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-12" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.provider') }}</label>
                <Select v-model="formData.provider">
                  <SelectTrigger data-testid="admin-model-backends-provider-select" aria-label="Provider" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm">
                    <SelectValue placeholder="anthropic" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="anthropic">{{ $t('views.AdminModelBackendsView.provider_anthropic') }}</SelectItem>
                    <SelectItem value="openai">{{ $t('views.AdminModelBackendsView.provider_openai') }}</SelectItem>
                    <SelectItem value="opencode">{{ $t('views.AdminModelBackendsView.provider_opencode') }}</SelectItem>
                    <SelectItem value="azure_openai">{{ $t('views.AdminModelBackendsView.azure_openai') }}</SelectItem>
                    <SelectItem value="ollama">{{ $t('views.AdminModelBackendsView.provider_ollama') }}</SelectItem>
                    <SelectItem value="groq">{{ $t('views.AdminModelBackendsView.provider_groq') }}</SelectItem>
                    <SelectItem value="deepseek">{{ $t('views.AdminModelBackendsView.provider_deepseek') }}</SelectItem>
                    <SelectItem value="gemini">{{ $t('views.AdminModelBackendsView.provider_gemini') }}</SelectItem>
                    <SelectItem value="mistral">{{ $t('views.AdminModelBackendsView.provider_mistral') }}</SelectItem>
                    <SelectItem value="cohere">{{ $t('views.AdminModelBackendsView.provider_cohere') }}</SelectItem>
                    <SelectItem value="togetherai">{{ $t('views.AdminModelBackendsView.provider_togetherai') }}</SelectItem>
                    <SelectItem value="fireworks">{{ $t('views.AdminModelBackendsView.provider_fireworks') }}</SelectItem>
                    <SelectItem value="openrouter">{{ $t('views.AdminModelBackendsView.provider_openrouter') }}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div v-if="showBaseUrl">
                <label for="adminmodelbackendsview-field-11" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.base_url') }}</label>
                <input id="adminmodelbackendsview-field-11"
                  v-model="formData.base_url"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  placeholder="https://api.example.com/v1"
                  data-testid="admin-model-backends-base-url-input"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-10" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.model_id') }}</label>
                <input id="adminmodelbackendsview-field-10"
                  v-model="formData.model_id"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  placeholder="claude-sonnet-4-20250514"
                  data-testid="admin-model-backends-model-id-input"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-9" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.api_key') }}</label>
                <input id="adminmodelbackendsview-field-9"
                  v-model="formData.api_key"
                  type="password"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  placeholder="sk-..."
                  data-testid="admin-model-backends-api-key-input"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-8" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.default_params_json') }}</label>
                <textarea id="adminmodelbackendsview-field-8"
                  v-model="formData.default_params"
                  rows="4"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                  placeholder='{ "temperature": 0.7, "max_tokens": 4096 }'
                  data-testid="admin-model-backends-params-input"
                ></textarea>
              </div>
              <div>
                <label for="adminmodelbackendsview-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.visibility') }}</label>
                <Select v-model="formData.visibility">
                  <SelectTrigger data-testid="admin-model-backends-visibility-select" aria-label="Visibility" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm">
                    <SelectValue placeholder="org" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="org">{{ $t('views.AdminModelBackendsView.visibility_org') }}</SelectItem>
                    <SelectItem value="private">{{ $t('views.AdminModelBackendsView.visibility_private') }}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div class="flex items-center gap-2">
              <Button
                :disabled="saving || !formData.name.trim() || !formData.display_name.trim() || !formData.model_id.trim() || !formData.api_key.trim()"
                type="submit"
                variant="default"
                data-testid="admin-model-backends-submit"
              >
                {{ saving ? $t('views.AdminModelBackendsView.creating') : $t('views.AdminModelBackendsView.create') }}
              </Button>
                <button
                  type="button"
                  class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                  data-testid="admin-model-backends-cancel"
                  @click="closeForm"
                >
                  {{ $t('views.AdminModelBackendsView.cancel') }}
                </button>
              </div>
            </div>
          </form>
        </div>

        <div v-if="nativeBackends.length === 0" class="card p-8 text-center">
          <p class="text-lg font-medium">{{ $t('views.AdminModelBackendsView.no_model_backends_configured') }}</p>
          <p class="mt-1 text-sm text-muted-foreground">
            {{ $t('views.AdminModelBackendsView.no_backends_description') }}
          </p>
        </div>

        <div v-else class="table-wrapper">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.name') }}</th>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.provider') }}</th>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.model_id') }}</th>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.display_name') }}</th>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.credentials') }}</th>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.visibility') }}</th>
                <th class="table-header table-cell-numeric">{{ $t('views.AdminModelBackendsView.actions') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr
                v-for="backend in nativeBackends"
                :key="backend.id"
                class="hover:bg-muted/30 transition-colors"
                :data-testid="`model-backend-row-${backend.id}`"
              >
                <td class="table-cell font-medium">{{ backend.name }}</td>
                <td class="table-cell">
                  <span class="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
                    {{ backend.provider }}
                  </span>
                </td>
                <td class="table-cell font-mono text-xs">{{ backend.model_id }}</td>
                <td class="table-cell text-muted-foreground">{{ backend.display_name }}</td>
                <td class="table-cell">
                  <span
                    class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
                    :class="backend.has_credentials ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                  >
                    <span
                      class="h-1.5 w-1.5 rounded-full"
                      :class="backend.has_credentials ? 'bg-success' : 'bg-muted-foreground'"
                    />
                    {{ backend.has_credentials ? $t('views.AdminModelBackendsView.configured') : $t('views.AdminModelBackendsView.missing') }}
                  </span>
                </td>
                <td class="table-cell text-xs text-muted-foreground">
                  {{ backend.visibility }}
                </td>
                <td class="table-cell-numeric">
                  <TableActions :actions="backendActions(backend)" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <details v-if="previewBackends.length > 0" class="rounded-lg border bg-card" data-testid="model-backends-preview-section">
          <summary class="cursor-pointer px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">
            {{ $t('views.AdminModelBackendsView.preview_model_backends_count', { count: previewBackends.length }, previewBackends.length) }}
          </summary>
          <div class="overflow-hidden border-t">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.name') }}</th>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.provider') }}</th>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.model_id') }}</th>
                <th class="table-header">{{ $t('views.AdminModelBackendsView.tier') }}</th>
                <th class="table-header table-cell-numeric">{{ $t('views.AdminModelBackendsView.actions') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr
                v-for="backend in previewBackends"
                :key="backend.id"
                class="hover:bg-muted/30 transition-colors"
                :data-testid="`model-backend-row-${backend.id}`"
              >
                <td class="table-cell font-medium">{{ backend.name }}</td>
                <td class="table-cell">
                  <span class="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
                    {{ backend.provider }}
                  </span>
                </td>
                <td class="table-cell font-mono text-xs">{{ backend.model_id }}</td>
                <td class="table-cell">
                  <span class="badge badge-context-amber text-xs">{{ $t('views.AdminModelBackendsView.preview_badge') }}</span>
                </td>
                <td class="table-cell-numeric">
                    <TableActions :actions="backendActions(backend)" />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>

        <div v-if="editBackendId" class="card p-6">
          <h2 class="mb-4 text-base font-semibold">{{ $t('views.AdminModelBackendsView.edit_model_backend') }}</h2>
          <form @submit.prevent="updateBackend">
            <div class="space-y-4">
              <div>
                <label for="adminmodelbackendsview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.name') }}</label>
                <input id="adminmodelbackendsview-field-6"
                  v-model="formData.name"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-model-backends-edit-name"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.display_name') }}</label>
                <input id="adminmodelbackendsview-field-5"
                  v-model="formData.display_name"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-model-backends-edit-display-name"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.model_id') }}</label>
                <input id="adminmodelbackendsview-field-4"
                  v-model="formData.model_id"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-model-backends-edit-model-id"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.api_key_leave_blank_to_keep_existing') }}</label>
                <input id="adminmodelbackendsview-field-3"
                  v-model="formData.api_key"
                  type="password"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminModelBackendsView.enter_new_key_to_replace')"
                  data-testid="admin-model-backends-edit-api-key"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.default_params_json') }}</label>
                <textarea id="adminmodelbackendsview-field-2"
                  v-model="formData.default_params"
                  rows="4"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                  data-testid="admin-model-backends-edit-params"
                ></textarea>
              </div>
              <div>
                <label for="adminmodelbackendsview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.visibility') }}</label>
                <Select v-model="formData.visibility">
                  <SelectTrigger data-testid="admin-model-backends-edit-visibility" aria-label="Visibility" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm">
                    <SelectValue placeholder="org" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="org">{{ $t('views.AdminModelBackendsView.visibility_org') }}</SelectItem>
                    <SelectItem value="private">{{ $t('views.AdminModelBackendsView.visibility_private') }}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div class="flex items-center gap-2">
              <Button
                :disabled="saving || !formData.name.trim() || !formData.display_name.trim() || !formData.model_id.trim()"
                type="submit"
                variant="default"
                data-testid="admin-model-backends-save"
              >
                {{ saving ? $t('views.AdminModelBackendsView.saving') : $t('views.AdminModelBackendsView.save') }}
              </Button>
                <button
                  type="button"
                  class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                  data-testid="admin-model-backends-edit-cancel"
                  @click="closeEditForm"
                >
                  {{ $t('views.AdminModelBackendsView.cancel') }}
                </button>
              </div>
            </div>
          </form>
        </div>

        <div v-if="deleteConfirmBackendId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
          <p class="text-sm font-medium text-destructive">{{ $t('views.AdminModelBackendsView.delete_confirm', { name: deleteConfirmName }) }}</p>
          <p class="mt-1 text-sm text-destructive/80">{{ $t('views.AdminModelBackendsView.this_action_cannot_be_undone') }}</p>
          <div class="mt-3 flex items-center gap-2">
          <Button
            :disabled="deleting"
            variant="destructive"
            data-testid="admin-model-backends-delete-confirm"
            @click="deleteBackend"
          >
            {{ deleting ? $t('views.AdminModelBackendsView.deleting') : $t('views.AdminModelBackendsView.delete') }}
          </Button>
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              data-testid="admin-model-backends-cancel"
              @click="closeForm"
            >
              {{ $t('views.AdminModelBackendsView.cancel') }}
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
import { ref, reactive, computed } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { formatApiError } from '../lib/api/formatError'
import { Button } from '@/components/ui/button'
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select'
import TableActions from '../components/shared/TableActions.vue'

type ModelBackendItem = components['schemas']['ModelBackendResponse']

interface BackendFormState {
  name: string
  display_name: string
  provider: string
  model_id: string
  api_key: string
  base_url: string
  default_params: string
  visibility: string
}

const variableBaseProviders = new Set(['azure_openai', 'ollama', 'openrouter', 'custom'])

const showBaseUrl = computed(() => variableBaseProviders.has(formData.provider))

function emptyForm(): BackendFormState {
  return {
    name: '',
    display_name: '',
    provider: 'anthropic',
    model_id: '',
    api_key: '',
    base_url: '',
    default_params: '',
    visibility: 'org',
  }
}

const { data: backendsResp, loading, error, load: loadBackends } = useDataFetch(
  () => api.GET('/api/v1/model-backends'),
  { initialValue: { items: [] } as { items: ModelBackendItem[] } }
)

const nativeBackends = computed(() => (backendsResp.value?.items ?? []).filter(b => (b.tier ?? 'native') !== 'preview' && (b.tier ?? 'native') !== 'in_dev'))
const previewBackends = computed(() => (backendsResp.value?.items ?? []).filter(b => b.tier === 'preview'))

const formMode = ref<'add' | 'edit' | null>(null)
const formData = reactive<BackendFormState>(emptyForm())
const editBackendId = ref<string | null>(null)

const saving = ref(false)
const formError = ref<string | null>(null)

const deleteConfirmBackendId = ref<string | null>(null)
const deleteConfirmName = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)

function openAddForm() {
  formMode.value = 'add'
  Object.assign(formData, emptyForm())
  editBackendId.value = null
  deleteConfirmBackendId.value = null
  formError.value = null
}

function openEditForm(backend: ModelBackendItem) {
  formMode.value = 'edit'
  editBackendId.value = backend.id
  deleteConfirmBackendId.value = null
  formError.value = null
  const params = (backend.default_params ?? {}) as Record<string, unknown>
  const baseUrl = typeof params.base_url === 'string' ? params.base_url : ''
  Object.assign(formData, {
    name: backend.name,
    display_name: backend.display_name,
    provider: backend.provider,
    model_id: backend.model_id,
    api_key: '',
    base_url: baseUrl,
    default_params: JSON.stringify(params, null, 2),
    visibility: backend.visibility,
  })
}

function closeForm() {
  formMode.value = null
  Object.assign(formData, emptyForm())
  formError.value = null
}

function closeEditForm() {
  editBackendId.value = null
  Object.assign(formData, emptyForm())
  formError.value = null
}

function buildDefaultParams(): Record<string, unknown> {
  const params: Record<string, unknown> = {}
  if (formData.default_params.trim()) {
    try {
      const parsed = JSON.parse(formData.default_params)
      if (typeof parsed === 'object' && parsed !== null) {
        Object.assign(params, parsed)
      }
    } catch (e) {
      console.warn('Failed to parse JSON default params', e)
    }
  }
  if (formData.base_url.trim()) {
    params.base_url = formData.base_url.trim()
  }
  return params
}

function buildCreateBody() {
  return {
    name: formData.name.trim(),
    display_name: formData.display_name.trim(),
    provider: formData.provider,
    model_id: formData.model_id.trim(),
    api_key: formData.api_key.trim(),
    default_params: buildDefaultParams(),
    visibility: formData.visibility,
  }
}

function buildUpdateBody() {
  const body: Record<string, unknown> = {
    name: formData.name.trim() || null,
    display_name: formData.display_name.trim() || null,
    model_id: formData.model_id.trim() || null,
    default_params: buildDefaultParams(),
    visibility: formData.visibility,
  }
  if (formData.api_key.trim()) {
    body.api_key = formData.api_key.trim()
  }
  return body
}

async function createBackend() {
  if (!formData.name.trim() || !formData.display_name.trim() || !formData.model_id.trim() || !formData.api_key.trim()) return
  saving.value = true
  formError.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/model-backends', {
      body: buildCreateBody() as any,
    })
    if (err) {
      formError.value = formatApiError(err)
    } else if (data) {
      closeForm()
      loadBackends()
    }
  } catch (e: unknown) {
    formError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

async function updateBackend() {
  if (!editBackendId.value || !formData.name.trim() || !formData.display_name.trim() || !formData.model_id.trim()) return
  saving.value = true
  formError.value = null
  try {
    const { data, error: err } = await api.PATCH('/api/v1/model-backends/{backend_id}', {
      params: { path: { backend_id: editBackendId.value } },
      body: buildUpdateBody() as any,
    })
    if (err) {
      formError.value = formatApiError(err)
    } else if (data) {
      closeEditForm()
      loadBackends()
    }
  } catch (e: unknown) {
    formError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

function confirmDelete(backend: ModelBackendItem) {
  deleteConfirmBackendId.value = backend.id
  deleteConfirmName.value = backend.display_name || backend.name
  editBackendId.value = null
  deleteError.value = null
}

async function deleteBackend() {
  if (!deleteConfirmBackendId.value) return
  deleting.value = true
  deleteError.value = null
  try {
    const { error: err, response } = await api.DELETE('/api/v1/model-backends/{backend_id}', {
      params: { path: { backend_id: deleteConfirmBackendId.value } },
    })
    if (err) {
      deleteError.value = formatApiError(err)
    } else if (response.status === 204 || response.ok) {
      deleteConfirmBackendId.value = null
      loadBackends()
    }
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  } finally {
    deleting.value = false
  }
}

function backendActions(backend: ModelBackendItem) {
  return [
    {
      key: 'edit',
      label: 'Edit',
      onClick: () => openEditForm(backend),
    },
    {
      key: 'delete',
      label: 'Delete',
      onClick: () => confirmDelete(backend),
      danger: true,
    },
  ]
}

/* onMounted handled by useDataFetch */
</script>
