<template>
  <FeatureGate feature-name="model_backend_management" show-disabled>
    <div class="page-wide">
      <header class="flex items-center justify-between">
        <PageHeader :title="$t('views.AdminModelBackendsView.model_backends')" :subtitle="$t('views.AdminModelBackendsView.manage_llm_backend_connections_and_credentials')" />
        <Button class="border-primary/30 hover:border-primary/60" data-testid="admin-model-backends-add" @click="openAddForm">
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
                <Select
  :aria-label="$t('views.AdminModelBackendsView.provider')"
  v-model="formData.provider"
  :placeholder="$t('views.AdminModelBackendsView.provider_placeholder')"
  data-testid="admin-model-backends-provider-select"
  class="w-full"
  :options="[{ value: 'anthropic', label: $t('views.AdminModelBackendsView.provider_anthropic') }, { value: 'openai', label: $t('views.AdminModelBackendsView.provider_openai') }, { value: 'opencode', label: $t('views.AdminModelBackendsView.provider_opencode') }, { value: 'azure_openai', label: $t('views.AdminModelBackendsView.azure_openai') }, { value: 'ollama', label: $t('views.AdminModelBackendsView.provider_ollama') }, { value: 'groq', label: $t('views.AdminModelBackendsView.provider_groq') }, { value: 'deepseek', label: $t('views.AdminModelBackendsView.provider_deepseek') }, { value: 'gemini', label: $t('views.AdminModelBackendsView.provider_gemini') }, { value: 'mistral', label: $t('views.AdminModelBackendsView.provider_mistral') }, { value: 'cohere', label: $t('views.AdminModelBackendsView.provider_cohere') }, { value: 'togetherai', label: $t('views.AdminModelBackendsView.provider_togetherai') }, { value: 'fireworks', label: $t('views.AdminModelBackendsView.provider_fireworks') }, { value: 'openrouter', label: $t('views.AdminModelBackendsView.provider_openrouter') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              </div>
              <div v-if="showBaseUrl">
                <label for="adminmodelbackendsview-field-11" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.base_url') }}</label>
                <input id="adminmodelbackendsview-field-11"
                  v-model="formData.base_url"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminModelBackendsView.base_url_placeholder')"
                  data-testid="admin-model-backends-base-url-input"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-10" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.model_id') }}</label>
                <input id="adminmodelbackendsview-field-10"
                  v-model="formData.model_id"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminModelBackendsView.model_id_placeholder')"
                  data-testid="admin-model-backends-model-id-input"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-9" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.api_key') }}</label>
                <input id="adminmodelbackendsview-field-9"
                  v-model="formData.api_key"
                  type="password"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminModelBackendsView.api_key_placeholder')"
                  data-testid="admin-model-backends-api-key-input"
                />
              </div>
              <div>
                <label for="adminmodelbackendsview-field-8" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.default_params_json') }}</label>
                <textarea id="adminmodelbackendsview-field-8"
                  v-model="formData.default_params"
                  rows="4"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                  :placeholder="$t('views.AdminModelBackendsView.default_params_placeholder')"
                  data-testid="admin-model-backends-params-input"
                ></textarea>
              </div>
              <div>
                <label for="adminmodelbackendsview-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.AdminModelBackendsView.visibility') }}</label>
                <Select
  :aria-label="$t('views.AdminModelBackendsView.visibility')"
  v-model="formData.visibility"
  :placeholder="$t('views.AdminModelBackendsView.visibility_placeholder')"
  data-testid="admin-model-backends-visibility-select"
  class="w-full"
  :options="[{ value: 'org', label: $t('views.AdminModelBackendsView.visibility_org') }, { value: 'private', label: $t('views.AdminModelBackendsView.visibility_private') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              </div>
              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div class="flex items-center gap-2">
              <Button :disabled="saving || !formData.name.trim() || !formData.display_name.trim() || !formData.model_id.trim() || !formData.api_key.trim()" type="submit" data-testid="admin-model-backends-submit">
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

        <EmptyState
          v-if="nativeBackends.length === 0"
          :title="$t('views.AdminModelBackendsView.no_model_backends_configured')"
          :description="$t('views.AdminModelBackendsView.no_backends_description')"
        />

        <div v-else class="table-wrapper overflow-x-auto">
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
              <template v-for="backend in nativeBackends" :key="backend.id">
                <tr
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
                  <td class="table-cell text-muted-foreground">{{ backend.display_name || '\u2014' }}</td>
                  <td class="table-cell">
                    <span
                      class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
                      :class="backend.has_credentials ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                    >
                      <span
                        aria-hidden="true"
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
                <tr>
                  <td colspan="7" class="p-0">
                    <details
                      :data-testid="`admin-model-backends-refs-expand-${backend.id}`"
                      @toggle="toggleRefsExpand(backend.id)"
                    >
                      <summary class="cursor-pointer px-4 py-2 text-xs text-muted-foreground hover:text-foreground">
                        {{ $t('views.AdminModelBackendsView.pipeline_references') }}
                      </summary>
                      <div class="border-t px-4 py-3" :data-testid="`admin-model-backends-refs-${backend.id}`">
                        <LoadingSpinner v-if="refsLoading && expandedBackendId === backend.id" />
                        <div v-else-if="refsError && expandedBackendId === backend.id" class="flex items-center gap-2 text-sm text-destructive">
                          <span>{{ refsError }}</span>
                          <button
                            class="rounded border border-input px-2 py-1 text-xs hover:bg-accent"
                            :data-testid="`admin-model-backends-refs-retry-${backend.id}`"
                            @click="fetchPipelineRefs(backend.id, 1)"
                          >
                            {{ $t('views.AdminModelBackendsView.retry') }}
                          </button>
                        </div>
                        <div v-else-if="expandedBackendId === backend.id && refsData">
                          <div v-if="refsData.items.length === 0" class="text-sm text-muted-foreground">
                            {{ $t('views.AdminModelBackendsView.no_pipeline_refs') }}
                          </div>
                          <template v-else>
                            <table class="w-full text-left text-xs" :data-testid="`admin-model-backends-refs-table-${backend.id}`">
                              <thead>
                                <tr>
                                  <th class="table-header">{{ $t('views.AdminModelBackendsView.pipeline_name') }}</th>
                                  <th class="table-header">{{ $t('views.AdminModelBackendsView.agent_name') }}</th>
                                  <th class="table-header">{{ $t('views.AdminModelBackendsView.reference_type') }}</th>
                                </tr>
                              </thead>
                              <tbody class="divide-y">
                                <tr v-for="ref in refsData.items" :key="`${ref.pipeline_id}-${ref.agent_id ?? 'direct'}`" class="hover:bg-muted/30">
                                  <td class="table-cell font-medium">{{ ref.pipeline_name }}</td>
                                  <td class="table-cell text-muted-foreground">{{ ref.agent_name || '\u2014' }}</td>
                                  <td class="table-cell">
                                    <span
                                      class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium"
                                      :class="ref.reference_type === 'direct_node' ? 'bg-primary/10 text-primary' : 'bg-warning/10 text-warning'"
                                    >
                                      {{ ref.reference_type === 'direct_node' ? $t('views.AdminModelBackendsView.ref_type_direct') : $t('views.AdminModelBackendsView.ref_type_agent') }}
                                    </span>
                                  </td>
                                </tr>
                              </tbody>
                            </table>
                            <div v-if="refsData.total > refsData.page_size" class="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                              <span>{{ $t('views.AdminModelBackendsView.refs_page_info', { page: refsData.page, total: Math.ceil(refsData.total / refsData.page_size) }) }}</span>
                              <button
                                v-if="refsData.page > 1"
                                class="rounded border border-input px-2 py-1 hover:bg-accent"
                                :data-testid="`admin-model-backends-refs-prev-${backend.id}`"
                                @click="fetchPipelineRefs(backend.id, refsData.page - 1)"
                              >
                                {{ $t('views.AdminModelBackendsView.previous') }}
                              </button>
                              <button
                                v-if="refsData.page < Math.ceil(refsData.total / refsData.page_size)"
                                class="rounded border border-input px-2 py-1 hover:bg-accent"
                                :data-testid="`admin-model-backends-refs-next-${backend.id}`"
                                @click="fetchPipelineRefs(backend.id, refsData.page + 1)"
                              >
                                {{ $t('views.AdminModelBackendsView.next') }}
                              </button>
                            </div>
                          </template>
                        </div>
                      </div>
                    </details>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>

        <details v-if="previewBackends.length > 0" class="rounded-lg border bg-card" data-testid="model-backends-preview-section">
          <summary class="cursor-pointer px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">
            {{ $t('views.AdminModelBackendsView.preview_model_backends_count', { count: previewBackends.length }, previewBackends.length) }}
          </summary>
          <div class="overflow-x-auto border-t">
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
                <Select
  :aria-label="$t('views.AdminModelBackendsView.visibility')"
  v-model="formData.visibility"
  :placeholder="$t('views.AdminModelBackendsView.visibility_placeholder')"
  data-testid="admin-model-backends-edit-visibility"
  class="w-full"
  :options="[{ value: 'org', label: $t('views.AdminModelBackendsView.visibility_org') }, { value: 'private', label: $t('views.AdminModelBackendsView.visibility_private') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              </div>
              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div class="flex items-center gap-2">
              <Button :disabled="saving || !formData.name.trim() || !formData.display_name.trim() || !formData.model_id.trim()" type="submit" data-testid="admin-model-backends-save">
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
          <Button :disabled="deleting" severity="danger" data-testid="admin-model-backends-delete-confirm" @click="deleteBackend">
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
import Button from 'primevue/button'
import Select from 'primevue/select'
import TableActions from '../components/shared/TableActions.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { useI18n } from 'vue-i18n'
type ModelBackendItem = components['schemas']['ModelBackendResponse']
type PipelineRefItem = components['schemas']['PipelineReference']

const { t } = useI18n()

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

const expandedBackendId = ref<string | null>(null)
const refsLoading = ref(false)
const refsError = ref<string | null>(null)
const refsData = ref<{ items: PipelineRefItem[]; total: number; page: number; page_size: number } | null>(null)

async function toggleRefsExpand(backendId: string) {
  if (expandedBackendId.value === backendId) {
    expandedBackendId.value = null
    refsData.value = null
    refsError.value = null
    return
  }
  expandedBackendId.value = backendId
  refsData.value = null
  refsError.value = null
  await fetchPipelineRefs(backendId, 1)
}

async function fetchPipelineRefs(backendId: string, page: number) {
  refsLoading.value = true
  refsError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/model-backends/{backend_id}/pipeline-references', {
      params: { path: { backend_id: backendId }, query: { page, page_size: 20 } },
    })
    if (err) {
      refsError.value = formatApiError(err)
    } else if (data) {
      refsData.value = data as { items: PipelineRefItem[]; total: number; page: number; page_size: number }
    }
  } catch (e: unknown) {
    refsError.value = formatApiError(e)
  } finally {
    refsLoading.value = false
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
    tier: 'native' as const,
  }
}

function buildUpdateBody(): components['schemas']['ModelBackendUpdate'] {
  const body: components['schemas']['ModelBackendUpdate'] = {
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
      body: buildCreateBody(),
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
      body: buildUpdateBody(),
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
      label: t('views.AdminModelBackendsView.edit_action'),
      onClick: () => openEditForm(backend),
    },
    {
      key: 'delete',
      label: t('views.AdminModelBackendsView.delete_action'),
      onClick: () => confirmDelete(backend),
      danger: true,
    },
  ]
}

/* onMounted handled by useDataFetch */
</script>
