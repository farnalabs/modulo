<template>
  <div class="page-wide">
    <FeatureGate feature-name="environment_profiles" required-tier="team" show-disabled>

      <header class="flex items-center justify-between">
        <PageHeader title="Environment Profiles" subtitle="Manage sandbox environment profiles for code execution" />
        <Button class="border-primary/30 hover:border-primary/60" data-testid="admin-envprofiles-add" @click="openAddForm">
          {{ $t('views.AdminEnvironmentProfilesView.create_profile') }}
        </Button>
      </header>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadProfiles" />

      <template v-else>
        <div v-if="formMode" class="card p-6">
          <h2 class="mb-4 text-base font-semibold">{{ formMode === 'add' ? $t('views.AdminEnvironmentProfilesView.new_environment_profile') : $t('views.AdminEnvironmentProfilesView.edit_environment_profile') }}</h2>
          <form @submit.prevent="formMode === 'add' ? createProfile() : updateProfile()">
            <div class="space-y-4">
              <div>
                <label for="adminenvironmentprofilesview-field-8" class="mb-1 block text-sm font-medium">{{ $t('views.AdminEnvironmentProfilesView.name') }}</label>
                <input id="adminenvironmentprofilesview-field-8"
                  v-model="formData.name"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminEnvironmentProfilesView.placeholder_my_profile')"
                  data-testid="admin-envprofiles-name-input"
                />
              </div>
              <div>
                <label for="adminenvironmentprofilesview-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.AdminEnvironmentProfilesView.description') }}</label>
                <input id="adminenvironmentprofilesview-field-7"
                  v-model="formData.description"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminEnvironmentProfilesView.placeholder_optional_description')"
                  data-testid="admin-envprofiles-description-input"
                />
              </div>
              <div>
                <label for="adminenvironmentprofilesview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.AdminEnvironmentProfilesView.provider_type') }}</label>
                <Select
  :aria-label="$t('views.AdminEnvironmentProfilesView.provider_type')"
  v-model="formData.provider_type"
  :placeholder="$t('views.AdminEnvironmentProfilesView.provider_e2b')"
  data-testid="admin-envprofiles-provider-select"
  class="w-full"
  :options="[{ value: 'e2b', label: $t('views.AdminEnvironmentProfilesView.provider_e2b') }, { value: 'docker', label: $t('views.AdminEnvironmentProfilesView.docker') }, { value: 'custom', label: $t('views.AdminEnvironmentProfilesView.custom_or_none') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              </div>
              <div v-if="formData.provider_type === 'custom'">
                <label for="adminenvironmentprofilesview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.AdminEnvironmentProfilesView.image_reference') }}</label>
                <input id="adminenvironmentprofilesview-field-5"
                  v-model="formData.image_ref"
                  type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="$t('views.AdminEnvironmentProfilesView.placeholder_image_ref')"
                  data-testid="admin-envprofiles-image-input"
                />
              </div>
              <div>
                <label for="adminenvironmentprofilesview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.AdminEnvironmentProfilesView.timeout_seconds') }}</label>
                <input id="adminenvironmentprofilesview-field-4"
                  v-model.number="formData.timeout_seconds"
                  type="number"
                  min="60"
                  max="86400"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  data-testid="admin-envprofiles-timeout-input"
                />
              </div>
              <div class="grid grid-cols-2 gap-4">
                <div>
                  <label for="adminenvironmentprofilesview-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.AdminEnvironmentProfilesView.memory_limit_mb') }}</label>
                  <input id="adminenvironmentprofilesview-field-3"
                    v-model.number="formData.memory_mb"
                    type="number"
                    min="128"
                    class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                    data-testid="admin-envprofiles-memory-input"
                  />
                </div>
                <div>
                  <label for="adminenvironmentprofilesview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.AdminEnvironmentProfilesView.cpu_cores') }}</label>
                  <input id="adminenvironmentprofilesview-field-2"
                    v-model.number="formData.cpu_cores"
                    type="number"
                    min="0.25"
                    step="0.25"
                    class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                    data-testid="admin-envprofiles-cpu-input"
                  />
                </div>
              </div>
              <div>
                <div class="mb-1 flex items-center justify-between">
                  <label for="adminenvironmentprofilesview-field-1" class="block text-sm font-medium">{{ $t('views.AdminEnvironmentProfilesView.environment_variables') }}</label>
                  <button
                    type="button"
                    class="text-xs text-primary hover:underline"
                    @click="addEnvVar"
                  >
                    {{ $t('views.AdminEnvironmentProfilesView.add_variable') }}
                  </button>
                </div>
                <div class="space-y-2">
                  <div
                    v-for="(env, idx) in formData.env_vars"
                    :key="idx"
                    class="flex items-center gap-2"
                  >
                    <input id="adminenvironmentprofilesview-field-1"
                      v-model="env.key"
                      type="text"
                      class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                      :placeholder="$t('views.AdminEnvironmentProfilesView.placeholder_key')"
                      :data-testid="`admin-envprofiles-env-key-${idx}`"
                    />
                    <input :aria-label="$t('views.AdminEnvironmentProfilesView.placeholder_value')"
                      v-model="env.value"
                      type="text"
                      class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                      :placeholder="$t('views.AdminEnvironmentProfilesView.placeholder_value')"
                      :data-testid="`admin-envprofiles-env-value-${idx}`"
                    />
                    <button
                      type="button"
                      class="rounded p-1 text-destructive hover:bg-destructive/10"
                      :data-testid="`admin-envprofiles-env-remove-${idx}`"
                      @click="removeEnvVar(idx)"
                      :aria-label="$t('views.AdminEnvironmentProfilesView.remove')"
                    >
                      <X class="h-4 w-4" />
                    </button>
                  </div>
                </div>
                <p v-if="formData.env_vars.length === 0" class="text-xs text-muted-foreground mt-1">{{ $t('views.AdminEnvironmentProfilesView.no_environment_variables_set') }}</p>
              </div>
              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div class="flex items-center gap-2">
              <Button :disabled="saving || !formData.name.trim()" type="submit" :data-testid="`admin-envprofiles-${formMode === 'add' ? 'submit' : 'save'}`">
                {{ saving ? $t('views.AdminEnvironmentProfilesView.saving') : (formMode === 'add' ? $t('views.AdminEnvironmentProfilesView.create') : $t('views.AdminEnvironmentProfilesView.save')) }}
              </Button>
                <button
                  type="button"
                  class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                  data-testid="admin-envprofiles-cancel"
                  @click="closeForm"
                >
                  {{ $t('views.AdminEnvironmentProfilesView.cancel') }}
                </button>
              </div>
            </div>
          </form>
        </div>

        <div v-if="profiles.length === 0" class="card p-8 text-center">
          <p class="text-lg font-medium">{{ $t('views.AdminEnvironmentProfilesView.no_environment_profiles_configured') }}</p>
          <p class="mt-1 text-sm text-muted-foreground">
            {{ $t('views.AdminEnvironmentProfilesView.empty_state_description') }}
          </p>
        </div>

      <div class="table-wrapper overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead>
            <tr>
              <th class="table-header">{{ $t('views.AdminEnvironmentProfilesView.name') }}</th>
              <th class="table-header">{{ $t('views.AdminEnvironmentProfilesView.provider') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.AdminEnvironmentProfilesView.timeout') }}</th>
              <th class="table-header capitalize">{{ $t('views.AdminEnvironmentProfilesView.status') }}</th>
              <th class="table-header">{{ $t('views.AdminEnvironmentProfilesView.created') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.AdminEnvironmentProfilesView.actions') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="profile in profiles"
              :key="profile.id"
              class="hover:bg-muted/30 transition-colors"
            >
              <td class="table-cell font-medium">{{ profile.name }}</td>
              <td class="table-cell">
                <span class="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
                  {{ providerLabel(profile) }}
                </span>
              </td>
              <td class="table-cell table-cell-numeric text-muted-foreground">{{ formatTimeout(profile.timeout_seconds ?? 0) }}</td>
              <td class="table-cell">
                <span
                  class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
                  :class="profile.is_active ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                >
                  <span
                    class="h-1.5 w-1.5 rounded-full"
                    :class="profile.is_active ? 'bg-success' : 'bg-muted-foreground'"
                  />
                  {{ profile.is_active ? $t('views.AdminEnvironmentProfilesView.active') : $t('views.AdminEnvironmentProfilesView.inactive') }}
                </span>
              </td>
              <td class="table-cell text-muted-foreground">{{ formatDate(profile.created_at) }}</td>
              <td class="table-cell-numeric">
                  <TableActions :actions="profileActions(profile)" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="testResult.profileId" class="card p-4">
          <div class="flex items-center justify-between mb-2">
            <h3 class="text-sm font-semibold">{{ $t('views.AdminEnvironmentProfilesView.test_connection') }}: {{ testResult.profileName }}</h3>
            <button type="button"
              class="text-xs text-muted-foreground hover:text-foreground"
              @click="closeTestResult"
            >
              {{ $t('views.AdminEnvironmentProfilesView.dismiss') }}
            </button>
          </div>
          <div class="space-y-1">
            <div
              v-for="(event, idx) in testResult.events"
              :key="idx"
              class="flex items-center gap-2 text-xs font-mono"
              :class="event.event === 'failed' ? 'text-destructive' : 'text-muted-foreground'"
            >
              <span
                class="inline-block h-2 w-2 rounded-full shrink-0"
                :class="{
                  'bg-yellow-400': event.event === 'provisioning' || event.event === 'destroying',
                  'bg-success': event.event === 'provisioned' || event.event === 'destroyed' || event.event === 'command_complete',
                  'bg-destructive': event.event === 'failed',
                  'bg-primary': event.event === 'command_start',
                }"
              />
              <span>{{ event.event }}</span>
              <span>{{ event.detail }}</span>
            </div>
          </div>
        </div>

        <div v-if="deleteConfirmProfileId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
          <p class="text-sm font-medium text-destructive">{{ $t('views.AdminEnvironmentProfilesView.delete_confirm', { name: deleteConfirmName }) }}</p>
          <p class="mt-1 text-sm text-destructive/80">{{ $t('views.AdminEnvironmentProfilesView.this_action_cannot_be_undone') }}</p>
          <div class="mt-3 flex items-center gap-2">
          <Button :disabled="deleting" severity="danger" data-testid="admin-envprofiles-delete-confirm" @click="deleteProfile">
            {{ deleting ? $t('views.AdminEnvironmentProfilesView.deleting') : $t('views.AdminEnvironmentProfilesView.delete') }}
          </Button>
            <button type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              data-testid="admin-envprofiles-delete-cancel"
              @click="deleteConfirmProfileId = null"
            >
              {{ $t('views.AdminEnvironmentProfilesView.cancel') }}
            </button>
          </div>
          <div v-if="deleteError" class="mt-2 text-sm text-destructive">{{ deleteError }}</div>
        </div>
      </template>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, reactive, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { api, getAccessToken } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import Button from 'primevue/button'
import Select from 'primevue/select'
import TableActions from '../components/shared/TableActions.vue'
import { formatDateShort } from '../lib/formatDate'
import { X } from '@lucide/vue'

type ProfileItem = components['schemas']['modulo__api__routes__environment_profiles__ProfileResponse'] & {
  timeout_seconds?: number
  is_active?: boolean
  resource_limits?: { memory_mb?: number; cpu_cores?: number }
  env_vars?: Record<string, string>
}

interface EnvVar {
  key: string
  value: string
}

interface ProfileFormState {
  name: string
  description: string
  provider_type: string
  image_ref: string
  timeout_seconds: number
  memory_mb: number
  cpu_cores: number
  env_vars: EnvVar[]
}

interface TestEvent {
  event: string
  detail: string
  timestamp: string
}

function emptyForm(): ProfileFormState {
  return {
    name: '',
    description: '',
    provider_type: 'e2b',
    image_ref: '',
    timeout_seconds: 3600,
    memory_mb: 512,
    cpu_cores: 1,
    env_vars: [],
  }
}

const { data: profilesData, loading, error, load: loadProfiles } = useDataFetch(
  () => api.GET('/api/v1/environments') as Promise<{ data?: { items?: ProfileItem[] }; error?: { detail?: string } }>,
  { initialValue: { items: [] as ProfileItem[] } }
)

const profiles = computed(() => {
  const d = profilesData.value
  return ((d as any)?.items ?? d ?? []) as ProfileItem[]
})

const formMode = ref<'add' | 'edit' | null>(null)
const formData = reactive<ProfileFormState>(emptyForm())
const editProfileId = ref<string | null>(null)

const saving = ref(false)
const formError = ref<string | null>(null)

const deleteConfirmProfileId = ref<string | null>(null)
const deleteConfirmName = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)

const testResult = reactive<{ profileId: string | null; profileName: string; events: TestEvent[] }>({
  profileId: null,
  profileName: '',
  events: [],
})

function providerLabel(profile: ProfileItem): string {
  if (profile.capabilities?.includes('provider:docker')) return 'Docker'
  if (profile.capabilities?.includes('provider:custom')) return 'Custom'
  return 'E2B'
}

function formatTimeout(seconds: number): string {
  if (seconds >= 3600) return `${Math.round(seconds / 3600)}h`
  if (seconds >= 60) return `${Math.round(seconds / 60)}m`
  return `${seconds}s`
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '—'
  try {
    return formatDateShort(new Date(dateStr))
  } catch {
    return '—'
  }
}

function openAddForm() {
  formMode.value = 'add'
  Object.assign(formData, emptyForm())
  editProfileId.value = null
  deleteConfirmProfileId.value = null
  formError.value = null
  testResult.profileId = null
}

function openEditForm(profile: ProfileItem) {
  formMode.value = 'edit'
  editProfileId.value = profile.id
  deleteConfirmProfileId.value = null
  formError.value = null
  testResult.profileId = null

  const provider = providerLabel(profile).toLowerCase()
  const memMb = profile.resource_limits?.memory_mb ?? 512
  const cpu = profile.resource_limits?.cpu_cores ?? 1
  const persistence = (profile as any).persistence ?? (profile as any).persistence_policy ?? {}
  const envVars: EnvVar[] = persistence.env_vars
    ? Object.entries(persistence.env_vars as Record<string, string>).map(([k, v]) => ({ key: k, value: v }))
    : []

  Object.assign(formData, {
    name: profile.name,
    description: profile.description ?? '',
    provider_type: provider,
    image_ref: profile.image_ref,
    timeout_seconds: profile.timeout_seconds,
    memory_mb: memMb,
    cpu_cores: cpu,
    env_vars: envVars,
  })
}

function closeForm() {
  formMode.value = null
  editProfileId.value = null
  Object.assign(formData, emptyForm())
  formError.value = null
}

function addEnvVar() {
  formData.env_vars.push({ key: '', value: '' })
}

function removeEnvVar(idx: number) {
  formData.env_vars.splice(idx, 1)
}

function buildProviderImageRef(provider_type: string, customRef: string): string {
  if (provider_type === 'custom') return customRef.trim() || 'custom/default'
  return `${provider_type}/default`
}

function buildCreateBody() {
  const providerType = formData.provider_type
  const envVarsObj: Record<string, string> = {}
  for (const env of formData.env_vars) {
    if (env.key.trim()) {
      envVarsObj[env.key.trim()] = env.value
    }
  }

  return {
    name: formData.name.trim(),
    description: formData.description.trim() || null,
    image_ref: buildProviderImageRef(providerType, formData.image_ref),
    capabilities: [`provider:${providerType}`],
    timeout_seconds: formData.timeout_seconds,
    resource_limits: {
      memory_mb: formData.memory_mb,
      cpu_cores: formData.cpu_cores,
    },
    persistence_policy: {
      env_vars: envVarsObj,
    },
  }
}

function buildUpdateBody() {
  const providerType = formData.provider_type
  const envVarsObj: Record<string, string> = {}
  for (const env of formData.env_vars) {
    if (env.key.trim()) {
      envVarsObj[env.key.trim()] = env.value
    }
  }

  return {
    name: formData.name.trim(),
    description: formData.description.trim() || null,
    image_ref: buildProviderImageRef(providerType, formData.image_ref),
    capabilities: [`provider:${providerType}`],
    timeout_seconds: formData.timeout_seconds,
    resource_limits: {
      memory_mb: formData.memory_mb,
      cpu_cores: formData.cpu_cores,
    },
    persistence_policy: {
      env_vars: envVarsObj,
    },
  }
}

async function createProfile() {
  if (!formData.name.trim()) return
  saving.value = true
  formError.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/environments', {
      body: buildCreateBody() as any,
    })
    if (err) {
      formError.value = String(err)
    } else if (data) {
      profiles.value.push(data as any)
      closeForm()
    }
  } catch (e: unknown) {
    formError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

async function updateProfile() {
  if (!editProfileId.value || !formData.name.trim()) return
  saving.value = true
  formError.value = null
  try {
    const { data, error: err } = await api.PATCH('/api/v1/environments/{profile_id}', {
      params: { path: { profile_id: editProfileId.value } },
      body: buildUpdateBody() as any,
    })
    if (err) {
      formError.value = String(err)
    } else if (data) {
      const idx = profiles.value.findIndex(p => p.id === editProfileId.value)
      if (idx >= 0) {
        profiles.value[idx] = data as any
      }
      closeForm()
    }
  } catch (e: unknown) {
    formError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

function confirmDelete(profile: ProfileItem) {
  deleteConfirmProfileId.value = profile.id
  deleteConfirmName.value = profile.name
  editProfileId.value = null
  deleteError.value = null
}

async function deleteProfile() {
  if (!deleteConfirmProfileId.value) return
  deleting.value = true
  deleteError.value = null
  try {
    const { error: err, response } = await api.DELETE('/api/v1/environments/{profile_id}', {
      params: { path: { profile_id: deleteConfirmProfileId.value } },
    })
    if (err) {
      deleteError.value = String(err)
    } else if (response.status === 204 || response.ok) {
      deleteConfirmProfileId.value = null
      await loadProfiles()
    }
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  } finally {
    deleting.value = false
  }
}

async function testConnection(profile: ProfileItem) {
  testResult.profileId = profile.id
  testResult.profileName = profile.name
  testResult.events = []

  try {
    const response = await fetch(`/api/v1/environments/${profile.id}/test`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${getAccessToken() ?? ''}`,
      },
    })
    if (!response.ok) {
      testResult.events.push({ event: 'failed', detail: `HTTP ${response.status}`, timestamp: new Date().toISOString() })
      return
    }

    const reader = response.body?.getReader()
    if (!reader) {
      testResult.events.push({ event: 'failed', detail: 'No response body', timestamp: new Date().toISOString() })
      return
    }

    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const parsed = JSON.parse(line.slice(6)) as TestEvent
            testResult.events.push(parsed)
          } catch {
            testResult.events.push({ event: 'info', detail: line.slice(6), timestamp: new Date().toISOString() })
          }
        }
      }
    }
  } catch (e: unknown) {
    testResult.events.push({ event: 'failed', detail: formatApiError(e), timestamp: new Date().toISOString() })
  }
}

function closeTestResult() {
  testResult.profileId = null
  testResult.profileName = ''
  testResult.events = []
}

const { t } = useI18n()

function profileActions(profile: ProfileItem) {
  return [
    {
      key: 'edit',
      label: t('views.AdminEnvironmentProfilesView.edit'),
      onClick: () => openEditForm(profile),
    },
    {
      key: 'test',
      label: t('views.AdminEnvironmentProfilesView.test'),
      onClick: () => testConnection(profile),
    },
    {
      key: 'delete',
      label: t('views.AdminEnvironmentProfilesView.delete'),
      onClick: () => confirmDelete(profile),
      danger: true,
    },
  ]
}

/* onMounted handled by useDataFetch */
</script>
