<template>
  <FeatureGate feature-name="sso" required-tier="team" show-disabled>

    <div class="page-wide">
      <header class="flex items-center justify-between">
        <PageHeader :title="$t('views.SettingsSsoView.title')" :subtitle="$t('views.SettingsSsoView.description')" />
        <Button class="border-primary/30 hover:border-primary/60" data-testid="settings-sso-add-provider" @click="openAddForm">
          {{ $t('views.SettingsSsoView.add_provider') }}
        </Button>
      </header>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadProviders" />

      <template v-else>
        <div v-if="formMode === 'add'" class="card max-w-3xl p-6">
          <h2 class="mb-4 text-base font-semibold">{{ $t('views.SettingsSsoView.new_sso_provider') }}</h2>
          <SsoProviderForm
            :data="formData"
            :saving="saving"
            :submit-label="$t('views.SettingsSsoView.create')"
            :saving-label="$t('views.SettingsSsoView.creating')"
            :error="formError"
            @update:data="onFormUpdate($event)"
            @submit="createProvider"
            @cancel="closeForm"
          />
        </div>

        <div v-if="providers.length === 0" class="card p-8 text-center">
          <p class="text-lg font-medium">{{ $t('views.SettingsSsoView.no_providers_title') }}</p>
          <p class="mt-1 text-sm text-muted-foreground">
            {{ $t('views.SettingsSsoView.no_providers_description') }}
          </p>
        </div>

        <div class="space-y-3">
          <div
            v-for="provider in providers"
            :key="provider.id"
            class="card"
          >
            <div class="flex items-center justify-between p-4">
              <div class="flex items-center gap-3">
                <div
                  class="flex h-10 w-10 items-center justify-center rounded-lg text-sm font-bold"
                  :class="provider.provider_type === 'oidc' ? 'badge badge-context-blue' : 'badge badge-context-amber'"
                >
                  {{ provider.provider_type === 'oidc' ? 'O' : 'S' }}
                </div>
                <div>
                  <p class="font-medium">{{ provider.name }}</p>
                  <p class="text-sm text-muted-foreground">
                    {{ provider.provider_type.toUpperCase() }}
                    <span v-if="provider.client_id" class="ml-2">&middot; {{ provider.client_id }}</span>
                    <span v-if="provider.entity_id" class="ml-2">&middot; {{ provider.entity_id }}</span>
                  </p>
                </div>
              </div>
              <div class="flex items-center gap-2">
                <button type="button"
                  :disabled="testingId === provider.id"
                  class="rounded-lg border border-input bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-50"
                  data-testid="settings-sso-test"
                  :title="$t('views.SettingsSsoView.test')"
                  @click="testConnection(provider.id)"
                >
                  {{ testingId === provider.id ? $t('views.SettingsSsoView.testing') : $t('views.SettingsSsoView.test') }}
                </button>
                <button type="button"
                  class="rounded p-1 text-muted-foreground hover:bg-accent"
                  data-testid="settings-sso-edit"
                  :aria-label="$t('views.SettingsSsoView.edit_provider')"
                  :title="$t('views.SettingsSsoView.edit_provider')"
                  @click="openEditForm(provider)"
                >
                  <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                  </svg>
                </button>
                <TableActions :actions="ssoActions(provider)" />
                <span tabindex="0"
                  class="relative inline-flex cursor-pointer items-center"
                  data-testid="settings-sso-toggle"
                  :aria-label="$t('views.SettingsSsoView.toggle_provider')"
                  role="switch"
                  :aria-checked="provider.enabled"
                  :class="{ 'opacity-50 pointer-events-none': togglingId === provider.id }"
                  @click.prevent.stop="toggleProvider(provider)"
                  @keydown.enter.prevent.stop="toggleProvider(provider)"
                  @keydown.space.prevent.stop="toggleProvider(provider)"
                >
                  <div
                    class="h-6 w-11 rounded-full transition-colors"
                    :class="provider.enabled ? 'bg-primary' : 'bg-input'"
                  >
                    <div
                      class="h-5 w-5 rounded-full bg-white shadow-sm transition-transform"
                      :class="provider.enabled ? 'translate-x-[1.375rem]' : 'translate-x-0.5'"
                      style="margin-top: 2px;"
                    />
                  </div>
                </span>
              </div>
            </div>

            <div v-if="editProviderId === provider.id" class="border-t p-4">
              <SsoProviderForm
                :data="formData"
                :saving="saving"
                :submit-label="$t('views.SettingsSsoView.save')"
                :saving-label="$t('views.SettingsSsoView.saving')"
                :error="formError"
                @update:data="onFormUpdate($event)"
                @submit="updateProvider"
                @cancel="closeEditForm"
              />
            </div>

            <div v-if="deleteConfirmProviderId === provider.id" class="border-t border-destructive/50 bg-destructive/10 p-4">
              <p class="text-sm font-medium text-destructive">{{ $t('views.SettingsSsoView.delete_confirm', { name: provider.name }) }}</p>
              <p class="mt-1 text-sm text-destructive/80">{{ $t('views.SettingsSsoView.delete_warning') }}</p>
              <div class="mt-3 flex items-center gap-2">
                <Button :disabled="deleting" severity="danger" data-testid="settings-sso-delete-confirm" @click="deleteProvider(provider.id)">
                  {{ deleting ? $t('views.SettingsSsoView.deleting') : $t('views.SettingsSsoView.delete') }}
                </Button>
                <button type="button"
                  class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                  data-testid="settings-sso-delete-cancel"
                  @click="deleteConfirmProviderId = null"
                >
                  {{ $t('views.SettingsSsoView.cancel') }}
                </button>
              </div>
              <div v-if="deleteError" class="mt-2 text-sm text-destructive">{{ deleteError }}</div>
            </div>

            <div v-if="testResultProviderId === provider.id" class="border-t p-4">
                <div
                  class="rounded-lg p-3 text-sm"
                  :class="testResult?.success ? 'bg-success/10 text-success' : 'bg-destructive/10 text-destructive'"
                >
                <p class="font-medium">{{ testResult?.success ? $t('views.SettingsSsoView.connection_successful') : $t('views.SettingsSsoView.connection_failed') }}</p>
                <p class="mt-1">{{ testResult?.message }}</p>
                <JsonViewer
                  v-if="testResult?.provider_info"
                  class="mt-2"
                  :data="testResult?.provider_info ?? null"
                  :show-toolbar="true"
                  :max-height="'16rem'"
                />
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, reactive, onBeforeUnmount } from 'vue'
import { useDataFetch } from '../composables/useDataFetch'
import Button from 'primevue/button'
import TableActions from '../components/shared/TableActions.vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import SsoProviderForm from '../components/SsoProviderForm.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { formatError } from '../lib/utils'
import { formatApiError } from '../lib/api/formatError'

type SsoProviderResponse = components['schemas']['SsoProviderResponse']
type SsoProviderCreate = components['schemas']['SsoProviderCreate']
type SsoProviderUpdate = components['schemas']['SsoProviderUpdate']
type SsoProviderTestResult = components['schemas']['SsoProviderTestResult']

interface SsoFormState {
  provider_type: string
  name: string
  client_id: string
  client_secret: string
  discovery_url: string
  metadata_url: string
  metadata_xml: string
  entity_id: string
  scopes: string
  auto_provision: boolean
  default_role: string
}

function emptyForm(): SsoFormState {
  return {
    provider_type: 'oidc',
    name: '',
    client_id: '',
    client_secret: '',
    discovery_url: '',
    metadata_url: '',
    metadata_xml: '',
    entity_id: '',
    scopes: '',
    auto_provision: true,
    default_role: 'runner',
  }
}

const { loading, error, data: providers, load: loadProviders } = useDataFetch<SsoProviderResponse[]>(
  async () => {
    const resp = await api.GET('/api/v1/admin/sso/providers')
    return { data: (Array.isArray(resp.data) ? resp.data : (resp.data as any)?.items ?? []) }
  },
  { initialValue: [] as SsoProviderResponse[] }
)

const formMode = ref<'add' | 'edit' | null>(null)
const formData = reactive<SsoFormState>(emptyForm())
const editProviderId = ref<string | null>(null)

const saving = ref(false)
const formError = ref<string | null>(null)

const deleteConfirmProviderId = ref<string | null>(null)
const deleting = ref(false)
const deleteError = ref<string | null>(null)

const togglingId = ref<string | null>(null)
const testingId = ref<string | null>(null)
const testResultProviderId = ref<string | null>(null)
const testResult = ref<SsoProviderTestResult | null>(null)
let ssoTestTimeout: ReturnType<typeof setTimeout> | null = null

function onFormUpdate(updated: SsoFormState) {
  Object.assign(formData, updated)
}

function openAddForm() {
  formMode.value = 'add'
  Object.assign(formData, emptyForm())
  editProviderId.value = null
  deleteConfirmProviderId.value = null
  testResultProviderId.value = null
  formError.value = null
}

function openEditForm(provider: SsoProviderResponse) {
  formMode.value = 'edit'
  editProviderId.value = provider.id
  deleteConfirmProviderId.value = null
  testResultProviderId.value = null
  formError.value = null
  Object.assign(formData, {
    provider_type: provider.provider_type,
    name: provider.name,
    client_id: provider.client_id ?? '',
    client_secret: '',
    discovery_url: provider.discovery_url ?? '',
    metadata_url: provider.metadata_url ?? '',
    metadata_xml: provider.metadata_xml ?? '',
    entity_id: provider.entity_id ?? '',
    scopes: (provider.scopes ?? []).join(', '),
    auto_provision: provider.auto_provision,
    default_role: provider.default_role,
  })
}

function closeForm() {
  formMode.value = null
  Object.assign(formData, emptyForm())
  formError.value = null
}

function closeEditForm() {
  editProviderId.value = null
  Object.assign(formData, emptyForm())
  formError.value = null
}

function buildCreateBody(): SsoProviderCreate {
  const scopes = formData.scopes
    ? formData.scopes.split(',').map(s => s.trim()).filter(Boolean)
    : []

  const base: SsoProviderCreate = {
    provider_type: formData.provider_type,
    name: formData.name.trim(),
    auto_provision: formData.auto_provision,
    default_role: formData.default_role,
    enabled: true,
  }

  if (formData.provider_type === 'oidc') {
    base.client_id = formData.client_id.trim() || null
    base.client_secret = formData.client_secret.trim() || null
    base.discovery_url = formData.discovery_url.trim() || null
    if (scopes.length > 0) base.scopes = scopes
  } else {
    base.metadata_url = formData.metadata_url.trim() || null
    base.metadata_xml = formData.metadata_xml.trim() || null
    base.entity_id = formData.entity_id.trim() || null
  }

  return base
}

function buildUpdateBody(): SsoProviderUpdate {
  const scopes = formData.scopes
    ? formData.scopes.split(',').map(s => s.trim()).filter(Boolean)
    : []

  const body: SsoProviderUpdate = {
    name: formData.name.trim() || null,
    auto_provision: formData.auto_provision ?? null,
    default_role: formData.default_role || null,
  }

  if (formData.provider_type === 'oidc') {
    body.client_id = formData.client_id.trim() || null
    if (formData.client_secret.trim()) body.client_secret = formData.client_secret.trim()
    body.discovery_url = formData.discovery_url.trim() || null
    body.scopes = scopes.length > 0 ? scopes : null
  } else {
    body.metadata_url = formData.metadata_url.trim() || null
    body.metadata_xml = formData.metadata_xml.trim() || null
    body.entity_id = formData.entity_id.trim() || null
  }

  return body
}

async function createProvider() {
  if (!formData.name.trim()) {
    formError.value = 'Provider name is required'
    return
  }
  saving.value = true
  formError.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/admin/sso/providers', {
      body: buildCreateBody(),
    })
    if (err) {
      formError.value = formatError(err)
    } else if (data) {
      providers.value.push(data)
      closeForm()
    }
  } catch (e: unknown) {
    formError.value = formatError(e)
  } finally {
    saving.value = false
  }
}

async function updateProvider() {
  if (!editProviderId.value) return
  if (!formData.name.trim()) {
    formError.value = 'Provider name is required'
    return
  }
  saving.value = true
  formError.value = null
  try {
    const { data, error: err } = await api.PUT('/api/v1/admin/sso/providers/{provider_id}', {
      params: { path: { provider_id: editProviderId.value } },
      body: buildUpdateBody(),
    })
    if (err) {
      formError.value = formatError(err)
    } else if (data) {
      const idx = providers.value.findIndex(p => p.id === editProviderId.value)
      if (idx >= 0) providers.value[idx] = data
      closeEditForm()
    }
  } catch (e: unknown) {
    formError.value = formatError(e)
  } finally {
    saving.value = false
  }
}

async function toggleProvider(provider: SsoProviderResponse) {
  togglingId.value = provider.id
  try {
    const { data, error: err } = await api.PUT('/api/v1/admin/sso/providers/{provider_id}/toggle', {
      params: { path: { provider_id: provider.id } },
    })
    if (err) {
      error.value = `Toggle failed: ${formatError(err)}`
    } else if (data) {
      const idx = providers.value.findIndex(p => p.id === provider.id)
      if (idx >= 0) providers.value[idx] = data
    }
  } catch (e: unknown) {
    error.value = `Toggle failed: ${formatApiError(e)}`
  } finally {
    togglingId.value = null
  }
}

function confirmDelete(provider: SsoProviderResponse) {
  deleteConfirmProviderId.value = provider.id
  editProviderId.value = null
  deleteError.value = null
}

async function deleteProvider(providerId: string) {
  deleting.value = true
  deleteError.value = null
  try {
    const { error: err, response } = await api.DELETE('/api/v1/admin/sso/providers/{provider_id}', {
      params: { path: { provider_id: providerId } },
    })
    if (err) {
      deleteError.value = formatError(err)
    } else if (response.status === 204 || response.ok) {
      providers.value = providers.value.filter(p => p.id !== providerId)
      deleteConfirmProviderId.value = null
    }
  } catch (e: unknown) {
    deleteError.value = formatError(e)
  } finally {
    deleting.value = false
  }
}

async function testConnection(providerId: string) {
  testingId.value = providerId
  testResultProviderId.value = providerId
  testResult.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/admin/sso/providers/{provider_id}/test', {
      params: { path: { provider_id: providerId } },
    })
    if (err) {
      testResult.value = { success: false, message: formatError(err), provider_info: null }
    } else if (data) {
      testResult.value = data
      if (ssoTestTimeout) clearTimeout(ssoTestTimeout)
      ssoTestTimeout = setTimeout(() => { testResultProviderId.value = null; testResult.value = null }, 12000)
    }
  } catch (e: unknown) {
    testResult.value = { success: false, message: formatError(e), provider_info: null }
  } finally {
    testingId.value = null
  }
}

onBeforeUnmount(() => {
  if (ssoTestTimeout) clearTimeout(ssoTestTimeout)
})
function ssoActions(provider: SsoProviderResponse) {
  return [
    {
      key: 'delete',
      label: 'Delete',
      onClick: () => confirmDelete(provider),
      danger: true,
    },
  ]
}


</script>
