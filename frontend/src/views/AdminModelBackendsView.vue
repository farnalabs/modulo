<template>
  <FeatureGate feature-name="model_backend_management">
    <div class="mx-auto max-w-4xl space-y-8 p-6">
      <header class="flex items-center justify-between">
        <div>
          <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.AdminModelBackendsView.model_backends') }}</h1>
          <p class="mt-1 text-muted-foreground">{{ $t('views.AdminModelBackendsView.manage_llm_backend_connections_and_credentials') }}</p>
        </div>
        <button
          class="btn-glow rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:border-primary/60 hover:brightness-110 transition-all duration-150"
          data-testid="admin-model-backends-add"
          @click="openAddForm"
        >
          Add Model Backend
        </button>
      </header>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadBackends" />

      <template v-else>
        <div v-if="formMode === 'add'" class="card p-6">
          <h2 class="mb-4 text-lg font-semibold">{{ $t('views.AdminModelBackendsView.new_model_backend') }}</h2>
          <form @submit.prevent="createBackend">
            <div class="space-y-4">
              <div>
                <label class="mb-1 block text-sm font-medium">Name</label>
                <input
                  v-model="formData.name"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  placeholder="my-llm-backend"
                  data-testid="admin-model-backends-name-input"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.display_name') }}</label>
                <input
                  v-model="formData.display_name"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminModelBackendsView.my_llm_backend')"
                  data-testid="admin-model-backends-display-name-input"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Provider</label>
                <select
                  v-model="formData.provider"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-model-backends-provider-select"
                >
                  <option value="anthropic">Anthropic</option>
                  <option value="openai">OpenAI</option>
                  <option value="azure_openai">{{ $t('views.AdminModelBackendsView.azure_openai') }}</option>
                  <option value="ollama">Ollama</option>
                  <option value="groq">Groq</option>
                  <option value="deepseek">DeepSeek</option>
                  <option value="google">Google</option>
                  <option value="mistral">Mistral</option>
                  <option value="cohere">Cohere</option>
                  <option value="together">Together</option>
                  <option value="fireworks">Fireworks</option>
                  <option value="replicate">Replicate</option>
                  <option value="openrouter">OpenRouter</option>
                  <option value="custom">Custom</option>
                </select>
              </div>
              <div v-if="showBaseUrl">
                <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.base_url') }}</label>
                <input
                  v-model="formData.base_url"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  placeholder="https://api.example.com/v1"
                  data-testid="admin-model-backends-base-url-input"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.model_id') }}</label>
                <input
                  v-model="formData.model_id"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  placeholder="claude-sonnet-4-20250514"
                  data-testid="admin-model-backends-model-id-input"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.api_key') }}</label>
                <input
                  v-model="formData.api_key"
                  type="password"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  placeholder="sk-..."
                  data-testid="admin-model-backends-api-key-input"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.default_params_json') }}</label>
                <textarea
                  v-model="formData.default_params"
                  rows="4"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                  placeholder='{ "temperature": 0.7, "max_tokens": 4096 }'
                  data-testid="admin-model-backends-params-input"
                ></textarea>
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Visibility</label>
                <select
                  v-model="formData.visibility"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-model-backends-visibility-select"
                >
                  <option value="org">Organisation</option>
                  <option value="private">Private</option>
                </select>
              </div>
              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div class="flex items-center gap-2">
                <button
                  :disabled="saving || !formData.name.trim() || !formData.display_name.trim() || !formData.model_id.trim() || !formData.api_key.trim()"
                  type="submit"
                  class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
                  data-testid="admin-model-backends-submit"
                >
                  {{ saving ? 'Creating...' : 'Create' }}
                </button>
                <button
                  type="button"
                  class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                  data-testid="admin-model-backends-cancel"
                  @click="closeForm"
                >
                  Cancel
                </button>
              </div>
            </div>
          </form>
        </div>

        <div v-if="backends.length === 0" class="card p-8 text-center">
          <p class="text-lg font-medium">{{ $t('views.AdminModelBackendsView.no_model_backends_configured') }}</p>
          <p class="mt-1 text-sm text-muted-foreground">
            Add a model backend to connect to an LLM provider.
          </p>
        </div>

        <div class="overflow-hidden rounded-lg border">
          <table class="w-full text-left text-sm">
            <thead class="bg-muted/50">
              <tr>
                <th class="px-4 py-3 font-medium">Name</th>
                <th class="px-4 py-3 font-medium">Provider</th>
                <th class="px-4 py-3 font-medium">{{ $t('views.AdminModelBackendsView.model_id') }}</th>
                <th class="px-4 py-3 font-medium">{{ $t('views.AdminModelBackendsView.display_name') }}</th>
                <th class="px-4 py-3 font-medium">Credentials</th>
                <th class="px-4 py-3 font-medium">Visibility</th>
                <th class="px-4 py-3 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr
                v-for="backend in backends"
                :key="backend.id"
                class="hover:bg-muted/30 transition-colors"
              >
                <td class="px-4 py-3 font-medium">{{ backend.name }}</td>
                <td class="px-4 py-3">
                  <span class="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
                    {{ backend.provider }}
                  </span>
                </td>
                <td class="px-4 py-3 font-mono text-xs">{{ backend.model_id }}</td>
                <td class="px-4 py-3 text-muted-foreground">{{ backend.display_name }}</td>
                <td class="px-4 py-3">
                  <span
                    class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
                    :class="backend.has_credentials ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                  >
                    <span
                      class="h-1.5 w-1.5 rounded-full"
                      :class="backend.has_credentials ? 'bg-success' : 'bg-muted-foreground'"
                    />
                    {{ backend.has_credentials ? 'Configured' : 'Missing' }}
                  </span>
                </td>
                <td class="px-4 py-3 text-xs text-muted-foreground">
                  {{ backend.visibility }}
                </td>
                <td class="px-4 py-3 text-right">
                  <div class="flex items-center justify-end gap-1">
                    <button
                      class="rounded p-1 text-muted-foreground hover:bg-accent"
                      data-testid="admin-model-backends-edit"
                      :aria-label="$t('views.AdminModelBackendsView.edit_model_backend_1')"
                      :title="$t('views.AdminModelBackendsView.edit_model_backend')"
                      @click="openEditForm(backend)"
                    >
                      <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                      </svg>
                    </button>
                    <button
                      class="rounded p-1 text-destructive hover:bg-destructive/10"
                      data-testid="admin-model-backends-delete"
                      :aria-label="$t('views.AdminModelBackendsView.delete_model_backend')"
                      :title="$t('views.AdminModelBackendsView.delete_model_backend_1')"
                      @click="confirmDelete(backend)"
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

        <div v-if="editBackendId" class="card p-6">
          <h2 class="mb-4 text-lg font-semibold">{{ $t('views.AdminModelBackendsView.edit_model_backend') }}</h2>
          <form @submit.prevent="updateBackend">
            <div class="space-y-4">
              <div>
                <label class="mb-1 block text-sm font-medium">Name</label>
                <input
                  v-model="formData.name"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-model-backends-edit-name"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.display_name') }}</label>
                <input
                  v-model="formData.display_name"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-model-backends-edit-display-name"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.model_id') }}</label>
                <input
                  v-model="formData.model_id"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-model-backends-edit-model-id"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.api_key_leave_blank_to_keep_existing') }}</label>
                <input
                  v-model="formData.api_key"
                  type="password"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminModelBackendsView.enter_new_key_to_replace')"
                  data-testid="admin-model-backends-edit-api-key"
                />
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.default_params_json') }}</label>
                <textarea
                  v-model="formData.default_params"
                  rows="4"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                  data-testid="admin-model-backends-edit-params"
                ></textarea>
              </div>
              <div>
                <label class="mb-1 block text-sm font-medium">Visibility</label>
                <select
                  v-model="formData.visibility"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-model-backends-edit-visibility"
                >
                  <option value="org">Organisation</option>
                  <option value="private">Private</option>
                </select>
              </div>
              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div class="flex items-center gap-2">
                <button
                  :disabled="saving || !formData.name.trim() || !formData.display_name.trim() || !formData.model_id.trim()"
                  type="submit"
                  class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
                  data-testid="admin-model-backends-save"
                >
                  {{ saving ? 'Saving...' : 'Save' }}
                </button>
                <button
                  type="button"
                  class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                  data-testid="admin-model-backends-edit-cancel"
                  @click="closeEditForm"
                >
                  Cancel
                </button>
              </div>
            </div>
          </form>
        </div>

        <div v-if="deleteConfirmBackendId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
          <p class="text-sm font-medium text-destructive">Delete "{{ deleteConfirmName }}"?</p>
          <p class="mt-1 text-sm text-destructive/80">{{ $t('views.AdminModelBackendsView.this_action_cannot_be_undone') }}</p>
          <div class="mt-3 flex items-center gap-2">
            <button
              :disabled="deleting"
              class="rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:brightness-110 disabled:opacity-50 transition-all"
              data-testid="admin-model-backends-delete-confirm"
              @click="deleteBackend"
            >
              {{ deleting ? 'Deleting...' : 'Delete' }}
            </button>
            <button
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              data-testid="admin-model-backends-delete-cancel"
              @click="deleteConfirmBackendId = null"
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
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'

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

const backends = ref<ModelBackendItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

const formMode = ref<'add' | 'edit' | null>(null)
const formData = reactive<BackendFormState>(emptyForm())
const editBackendId = ref<string | null>(null)

const saving = ref(false)
const formError = ref<string | null>(null)

const deleteConfirmBackendId = ref<string | null>(null)
const deleteConfirmName = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)

async function loadBackends() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/model-backends')
    if (err) {
      error.value = `Failed to load model backends: ${err}`
    } else if (data) {
      backends.value = data.items
    }
  } catch (e: unknown) {
    error.value = `Failed to load model backends: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

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
  const params = backend.default_params as Record<string, unknown>
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
    } catch {
      // pass through as string
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
      formError.value = String(err)
    } else if (data) {
      backends.value.push(data)
      closeForm()
    }
  } catch (e: unknown) {
    formError.value = e instanceof Error ? e.message : String(e)
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
      formError.value = String(err)
    } else if (data) {
      const idx = backends.value.findIndex(b => b.id === editBackendId.value)
      if (idx >= 0) {
        backends.value[idx] = data
      }
      closeEditForm()
    }
  } catch (e: unknown) {
    formError.value = e instanceof Error ? e.message : String(e)
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
      deleteError.value = String(err)
    } else if (response.status === 204 || response.ok) {
      backends.value = backends.value.filter(b => b.id !== deleteConfirmBackendId.value)
      deleteConfirmBackendId.value = null
    }
  } catch (e: unknown) {
    deleteError.value = e instanceof Error ? e.message : String(e)
  } finally {
    deleting.value = false
  }
}

onMounted(loadBackends)
</script>
