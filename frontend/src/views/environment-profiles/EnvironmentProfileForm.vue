<template>
  <div class="page-narrow">
    <header class="flex items-center gap-3 mb-6">
      <button
        class="rounded p-1 text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
        data-testid="envprofile-form-back"
        @click="$router.push('/environment-profiles')"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M19 12H5" /><path d="m12 19-7-7 7-7" />
        </svg>
      </button>
      <PageHeader
        :title="isEdit ? 'Edit Environment Profile' : 'New Environment Profile'"
        :subtitle="isEdit ? 'Update the sandbox environment template' : 'Define a reusable sandbox environment template'"
      />
    </header>

    <form @submit.prevent="handleSubmit" class="card p-6 space-y-5">
      <div>
        <label for="environmentprofileform-field-7" class="mb-1 block text-sm font-medium">Name <span class="text-destructive">*</span></label>
        <input id="environmentprofileform-field-7"
          v-model="form.name"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          placeholder="e.g. python-dev"
          data-testid="envprofile-form-name"
          :class="{ 'border-destructive': submitted && !form.name.trim() }"
        />
        <p v-if="submitted && !form.name.trim()" class="mt-1 text-xs text-destructive">Name is required</p>
      </div>

      <div>
        <label for="environmentprofileform-field-6" class="mb-1 block text-sm font-medium">Description</label>
        <textarea id="environmentprofileform-field-6"
          v-model="form.description"
          rows="3"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          placeholder="Optional description of this environment template"
          data-testid="envprofile-form-description"
        />
      </div>

      <div>
        <label for="environmentprofileform-field-5" class="mb-1 block text-sm font-medium">Provider Type <span class="text-destructive">*</span></label>
        <select id="environmentprofileform-field-5"
          v-model="form.provider_type"
          aria-label="Provider type"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          data-testid="envprofile-form-provider"
          :class="{ 'border-destructive': submitted && !form.provider_type }"
        >
          <option value="local_docker">Local Docker</option>
          <option value="e2b">E2B (Sandboxed Cloud)</option>
        </select>
        <p v-if="submitted && !form.provider_type" class="mt-1 text-xs text-destructive">Provider type is required</p>
      </div>

      <div>
        <label for="environmentprofileform-field-4" class="mb-1 block text-sm font-medium">Image Reference</label>
        <input id="environmentprofileform-field-4"
          v-model="form.image_ref"
          type="text"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          placeholder="e.g. python:3.12-slim, node:20-bookworm"
          data-testid="envprofile-form-image"
        />
      </div>

      <div>
        <span class="mb-1 block text-sm font-medium">Capabilities</span>
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
        <label for="environmentprofileform-field-3" class="mb-1 block text-sm font-medium">Network Policy</label>
        <select id="environmentprofileform-field-3"
          v-model="form.network_policy"
          aria-label="Network policy"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          data-testid="envprofile-form-network"
        >
          <option value="outbound">Outbound (full egress)</option>
          <option value="none">None (isolated)</option>
          <option value="selected">Selected domains only</option>
        </select>
      </div>

      <div>
        <label for="environmentprofileform-field-2" class="mb-1 block text-sm font-medium">Initialisation Strategy</label>
        <select id="environmentprofileform-field-2"
          v-model="form.initialisation_strategy"
          aria-label="Initialisation strategy"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          data-testid="envprofile-form-init"
        >
          <option value="git_clone">Git Clone</option>
          <option value="blank">Blank (empty workspace)</option>
          <option value="worktree">Git Worktree</option>
        </select>
      </div>

      <div>
        <label for="environmentprofileform-field-1" class="mb-1 block text-sm font-medium">Persistence Policy</label>
        <select id="environmentprofileform-field-1"
          v-model="form.persistence_policy"
          aria-label="Persistence policy"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          data-testid="envprofile-form-persistence"
        >
          <option value="ephemeral">Ephemeral (destroyed after run)</option>
          <option value="retained">Retained (available for inspection)</option>
          <option value="cache">Cache (reusable between runs)</option>
        </select>
      </div>

      <div v-if="store.error" class="text-sm text-destructive">{{ store.error }}</div>
      <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>

      <div class="flex items-center gap-2 pt-2">
        <Button
          :disabled="store.isSaving"
          type="submit"
          variant="default"
          data-testid="envprofile-form-submit"
        >
          {{ store.isSaving ? 'Saving...' : (isEdit ? 'Save Changes' : 'Create Profile') }}
        </Button>
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors"
          data-testid="envprofile-form-cancel"
          @click="$router.push('/environment-profiles')"
        >
          Cancel
        </button>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../../components/shared/PageHeader.vue'
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useEnvironmentProfilesStore } from '../../stores/environmentProfiles'
import { Button } from '@/components/ui/button'

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
  provider_type: 'local_docker',
  image_ref: '',
  capabilities: [] as string[],
  network_policy: 'outbound',
  initialisation_strategy: 'git_clone',
  persistence_policy: 'ephemeral',
})

const submitted = ref(false)
const formError = ref<string | null>(null)

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

  const payload: Record<string, unknown> = {
    name: form.name.trim(),
    description: form.description.trim() || null,
    provider_type: form.provider_type,
    image_ref: form.image_ref.trim() || null,
    capabilities: [...form.capabilities],
    network_policy: form.network_policy,
    initialisation_strategy: form.initialisation_strategy,
    persistence_policy: form.persistence_policy,
  }

  try {
    const profileId = props.profileId || (route.params.id as string)
    if (profileId && profileId !== 'new') {
      await store.updateProfile(profileId, payload as any)
    } else {
      await store.createProfile(payload as any)
    }
    router.push('/environment-profiles')
  } catch (e: unknown) {
    formError.value = e instanceof Error ? e.message : 'Failed to save profile'
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
    }
  }
})
</script>
