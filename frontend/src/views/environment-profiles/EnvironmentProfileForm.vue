<template>
  <div class="page-wide">
    <header class="flex items-center gap-3 mb-6">
      <button type="button"
        class="rounded p-1 text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
        data-testid="envprofile-form-back"
        :aria-label="$t('views.EnvironmentProfileForm.back_to_profiles')"
        @click="$router.push('/environment-profiles')"
      >
        <ArrowLeft :size="18" aria-hidden="true" />
      </button>
      <PageHeader
        :title="isEdit ? $t('views.EnvironmentProfileForm.edit_title') : $t('views.EnvironmentProfileForm.create_title')"
        :subtitle="isEdit ? $t('views.EnvironmentProfileForm.edit_subtitle') : $t('views.EnvironmentProfileForm.create_subtitle')"
      />
    </header>

    <form @submit.prevent="handleSubmit" class="card max-w-3xl p-6 space-y-5">
      <div>
        <label for="environmentprofileform-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.name') }} <span class="text-destructive">*</span></label>
        <input id="environmentprofileform-field-7"
          v-model="form.name"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          :placeholder="$t('views.EnvironmentProfileForm.name_placeholder')"
          data-testid="envprofile-form-name"
          :class="{ 'border-destructive': submitted && !form.name.trim() }"
        />
        <p v-if="submitted && !form.name.trim()" class="mt-1 text-xs text-destructive">{{ $t('views.EnvironmentProfileForm.name_is_required') }}</p>
      </div>

      <div>
        <label for="environmentprofileform-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.description') }}</label>
        <textarea id="environmentprofileform-field-6"
          v-model="form.description"
          rows="3"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          :placeholder="$t('views.EnvironmentProfileForm.description_placeholder')"
          data-testid="envprofile-form-description"
        />
      </div>

      <div>
        <label for="environmentprofileform-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.provider_type') }} <span class="text-destructive">*</span></label>
        <Select
  :aria-label="$t('views.EnvironmentProfileForm.provider_type')"
  v-model="form.provider_type"
  :placeholder="$t('views.EnvironmentProfileForm.select_provider_type')"
  data-testid="envprofile-form-provider"
  class="w-full"
  :options="[{ value: 'local_docker', label: $t('views.EnvironmentProfileForm.local_docker') }, { value: 'e2b', label: $t('views.EnvironmentProfileForm.e2b_sandboxed_cloud') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        <p v-if="submitted && !form.provider_type" class="mt-1 text-xs text-destructive">{{ $t('views.EnvironmentProfileForm.provider_type_is_required') }}</p>
        <p v-if="formTierLabel" class="mt-1 text-xs text-muted-foreground">
          <span
            class="mr-1 inline-block rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
            data-testid="envprofile-form-tier-badge"
          >{{ formTierLabel }}</span>
        </p>
      </div>

      <div>
        <label for="environmentprofileform-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.image_reference') }}</label>
        <input id="environmentprofileform-field-4"
          v-model="form.image_ref"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          :placeholder="$t('views.EnvironmentProfileForm.image_reference_placeholder')"
          data-testid="envprofile-form-image"
        />
      </div>

      <div>
        <span class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.capabilities') }}</span>
        <div class="flex flex-wrap gap-2">
          <label
            v-for="cap in availableCapabilities"
            :key="cap"
            class="inline-flex items-center gap-1.5 rounded-lg border border-input px-3 py-1.5 text-sm cursor-pointer transition-colors"
            :class="form.capabilities.includes(cap) ? 'bg-primary/10 border-primary/30 text-primary' : 'hover:bg-accent'"
          >
            <input
              type="checkbox"
              :value="cap"
              :checked="form.capabilities.includes(cap)"
              class="sr-only"
              @change="toggleCapability(cap)"
            />
            {{ cap }}
          </label>
        </div>
      </div>

      <div>
        <label for="environmentprofileform-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.network_policy') }}</label>
        <Select
  :aria-label="$t('views.EnvironmentProfileForm.network_policy')"
  v-model="form.network_policy"
  :placeholder="$t('views.EnvironmentProfileForm.select_network_policy')"
  data-testid="envprofile-form-network"
  class="w-full"
  :options="[{ value: 'outbound', label: $t('views.EnvironmentProfileForm.outbound_full_egress') }, { value: 'none', label: $t('views.EnvironmentProfileForm.none_isolated') }, { value: 'selected', label: $t('views.EnvironmentProfileForm.selected_domains_only') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
      </div>

      <div>
        <label for="environmentprofileform-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.initialisation_strategy') }}</label>
        <Select
  :aria-label="$t('views.EnvironmentProfileForm.initialisation_strategy')"
  v-model="form.initialisation_strategy"
  :placeholder="$t('views.EnvironmentProfileForm.select_initialisation_strategy')"
  data-testid="envprofile-form-init"
  class="w-full"
  :options="[{ value: 'git_clone', label: $t('views.EnvironmentProfileForm.git_clone') }, { value: 'blank', label: $t('views.EnvironmentProfileForm.blank_empty_workspace') }, { value: 'worktree', label: $t('views.EnvironmentProfileForm.git_worktree') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
      </div>

      <div>
        <label for="environmentprofileform-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.EnvironmentProfileForm.persistence_policy') }}</label>
        <Select
  :aria-label="$t('views.EnvironmentProfileForm.persistence_policy')"
  v-model="form.persistence_policy"
  :placeholder="$t('views.EnvironmentProfileForm.select_persistence_policy')"
  data-testid="envprofile-form-persistence"
  class="w-full"
  :options="[{ value: 'ephemeral', label: $t('views.EnvironmentProfileForm.ephemeral_destroyed_after_run') }, { value: 'retained', label: $t('views.EnvironmentProfileForm.retained_available_for_inspection') }, { value: 'cache', label: $t('views.EnvironmentProfileForm.cache_reusable_between_runs') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
      </div>

      <div v-if="store.error" class="text-sm text-destructive">{{ store.error }}</div>
      <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>

      <div class="flex items-center gap-2 pt-2">
        <Button :disabled="store.isSaving" type="submit" data-testid="envprofile-form-submit">
          {{ store.isSaving
            ? $t('views.EnvironmentProfileForm.saving')
            : (isEdit ? $t('views.EnvironmentProfileForm.save_changes') : $t('views.EnvironmentProfileForm.create_profile')) }}
        </Button>
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors"
          data-testid="envprofile-form-cancel"
          @click="$router.push('/environment-profiles')"
        >
          {{ $t('views.EnvironmentProfileForm.cancel') }}
        </button>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../../components/shared/PageHeader.vue'
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useEnvironmentProfilesStore } from '../../stores/environmentProfiles'
import type { ProfileCreatePayload } from '../../stores/environmentProfiles'
import { runnerTierForProvider, runnerTierLabelKey } from '../../lib/runnerTiers'
import { ArrowLeft } from '@lucide/vue'
import Button from 'primevue/button'
import Select from '../../components/shared/AppSelect.vue'

const props = defineProps<{
  profileId?: string
}>()

const route = useRoute()
const router = useRouter()
const store = useEnvironmentProfilesStore()

const isEdit = computed(() => {
  const id = props.profileId || (route.params.id as string)
  return !!(id && id !== 'new')
})

const { t } = useI18n()

const availableCapabilities = [
  'git',
  'python>=3.12',
  'node>=18',
  'shell',
  'network:github.com',
  'network:pypi.org',
]

const form = reactive({
  name: '',
  description: '',
  provider_type: '',
  image_ref: '',
  capabilities: [] as string[],
  network_policy: 'outbound',
  initialisation_strategy: 'git_clone',
  persistence_policy: 'ephemeral',
})

const submitted = ref(false)
const formError = ref<string | null>(null)
// The form does not expose visibility (it is an org/team policy set elsewhere),
// but the update payload must echo the loaded profile's value: the backend
// applies `exclude_unset` updates, so hardcoding 'org' would silently flip a
// team-scoped profile to org while its owner_team_id survived.
const loadedVisibility = ref('org')

const formTierLabel = computed(() => {
  const tier = runnerTierForProvider(form.provider_type)
  return tier ? t(runnerTierLabelKey(tier)) : null
})

function toggleCapability(cap: string) {
  const idx = form.capabilities.indexOf(cap)
  if (idx >= 0) {
    form.capabilities.splice(idx, 1)
  } else {
    form.capabilities.push(cap)
  }
}

async function handleSubmit() {
  submitted.value = true
  formError.value = null

  if (!form.name.trim()) return
  if (!form.provider_type) return

  const profileId = props.profileId || (route.params.id as string)
  const isUpdate = !!(profileId && profileId !== 'new')

  const payload: ProfileCreatePayload = {
    name: form.name.trim(),
    description: form.description.trim() || null,
    provider_type: form.provider_type,
    image_ref: form.image_ref.trim() || null,
    capabilities: [...form.capabilities],
    network_policy: form.network_policy,
    initialisation_strategy: form.initialisation_strategy,
    persistence_policy: form.persistence_policy,
    visibility: isUpdate ? loadedVisibility.value : 'org',
  }

  try {
    if (isUpdate) {
      await store.updateProfile(profileId, payload)
    } else {
      await store.createProfile(payload)
    }
    router.push('/environment-profiles')
  } catch (e: unknown) {
    formError.value = e instanceof Error ? e.message : t('views.EnvironmentProfileForm.save_failed')
  }
}

onMounted(async () => {
  const profileId = props.profileId || (route.params.id as string)
  if (profileId && profileId !== 'new') {
    await store.fetchProfile(profileId)
    if (store.currentProfile) {
      const p = store.currentProfile
      form.name = p.name
      form.description = p.description ?? ''
      form.provider_type = p.provider_type
      form.image_ref = p.image_ref ?? ''
      form.capabilities = [...p.capabilities]
      form.network_policy = p.network_policy
      form.initialisation_strategy = p.initialisation_strategy
      form.persistence_policy = p.persistence_policy
      loadedVisibility.value = p.visibility
    }
  }
})
</script>
