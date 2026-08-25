<template>
  <div class="page-wide">
    <FeatureGate feature-name="environment_profiles" required-tier="team" show-disabled>

      <PageHeader title="Environment Profiles" subtitle="Reusable sandbox environment templates for code execution nodes" />

      <div class="space-y-6">
        <div class="flex items-center justify-between gap-3">
          <div class="relative">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input aria-label="Search profiles..."
              v-model="search"
              type="text"
              placeholder="Search profiles..."
              class="pl-9 pr-3 py-1.5 border border-input bg-background rounded-lg text-sm w-64"
              data-testid="envprofile-list-search"
            />
          </div>
          <Button class="border-primary/30 hover:border-primary/60" data-testid="envprofile-list-new" @click="$router.push('/environment-profiles/new')">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="mr-1"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            New Profile
          </Button>
        </div>

        <LoadingSpinner v-if="store.isLoading" />

        <ErrorAlert v-else-if="store.error" :message="store.error" :on-retry="store.fetchProfiles" />

        <template v-else-if="filteredProfiles.length === 0">
          <div v-if="search" class="card p-8 text-center">
            <p class="text-lg font-medium">{{ $t('views.EnvironmentProfileList.no_profiles_match', { search }) }}</p>
            <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.EnvironmentProfileList.try_a_different_search_term') }}</p>
          </div>
          <div v-else class="card p-8 text-center">
            <p class="text-lg font-medium">{{ $t('views.EnvironmentProfileList.no_environment_profiles') }}</p>
            <p class="mt-1 text-sm text-muted-foreground">
              Create one to define a sandbox template for pipeline nodes that need shell access.
            </p>
            <Button class="mt-4" data-testid="envprofile-list-empty-create" @click="$router.push('/environment-profiles/new')">
              Create Profile
            </Button>
          </div>
        </template>

        <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 xl:max-w-[1400px] mx-auto">
          <div
            v-for="profile in filteredProfiles"
            :key="profile.id"
            class="card p-5 flex flex-col gap-3"
          >
            <div class="flex items-start justify-between">
              <div>
                <router-link
                  :to="`/environment-profiles/${profile.id}`"
                  class="font-semibold text-sm hover:text-primary transition-colors"
                  data-testid="envprofile-list-name"
                >
                  {{ profile.name }}
                </router-link>
                <p v-if="profile.description" class="mt-0.5 text-xs text-muted-foreground line-clamp-2">{{ profile.description }}</p>
              </div>
              <span
                class="shrink-0 inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
                :class="profile.status === 'active' ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
              >
                <span
                  class="h-1.5 w-1.5 rounded-full"
                  :class="profile.status === 'active' ? 'bg-success' : 'bg-muted-foreground'"
                />
                <span class="capitalize">{{ profile.status === 'active' ? $t('common.active') : $t('common.deleted') }}</span>
              </span>
            </div>

            <div class="flex flex-wrap gap-1.5">
              <span class="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                {{ profile.provider_type }}
              </span>
            </div>

            <div class="flex items-center gap-2 mt-auto">
              <Button severity="secondary" outlined size="small" data-testid="envprofile-list-edit" @click="$router.push(`/environment-profiles/${profile.id}/edit`)">
                Edit
              </Button>
              <button type="button"
                class="ml-auto rounded p-1 text-destructive hover:bg-destructive/10 transition-colors"
                data-testid="envprofile-list-delete"
                :aria-label="'Delete profile'"
                @click="confirmDelete(profile)"
              >
                <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
                </svg>
              </button>
            </div>
          </div>
        </div>

        <div v-if="deleteConfirmId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
          <p class="text-sm font-medium text-destructive">Delete "{{ deleteConfirmName }}"?</p>
          <p class="mt-1 text-sm text-destructive/80">{{ $t('views.EnvironmentProfileList.soft_delete_warning') }}</p>
          <div class="mt-3 flex items-center gap-2">
            <Button :disabled="deleting" severity="danger" size="small" data-testid="envprofile-list-delete-confirm" @click="doDelete">
              {{ deleting ? 'Deleting...' : 'Delete' }}
            </Button>
            <button type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              data-testid="envprofile-list-delete-cancel"
              @click="deleteConfirmId = null"
            >
              Cancel
            </button>
          </div>
          <div v-if="deleteError" class="mt-2 text-sm text-destructive">{{ deleteError }}</div>
        </div>

      </div>

    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../../components/shared/PageHeader.vue'
import { ref, computed, onMounted } from 'vue'
import { useEnvironmentProfilesStore } from '../../stores/environmentProfiles'
import type { EnvironmentProfileSummary } from '../../stores/environmentProfiles'
import LoadingSpinner from '../../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'
import FeatureGate from '../../components/FeatureGate.vue'
import Button from 'primevue/button'

const store = useEnvironmentProfilesStore()

const search = ref('')
const deleteConfirmId = ref<string | null>(null)
const deleteConfirmName = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)

const filteredProfiles = computed(() => {
  if (!search.value) return store.profiles
  const q = search.value.toLowerCase()
  return store.profiles.filter(
    (p) =>
      p.name.toLowerCase().includes(q) ||
      (p.description ?? '').toLowerCase().includes(q) ||
      p.provider_type.toLowerCase().includes(q)
  )
})

function confirmDelete(profile: EnvironmentProfileSummary) {
  deleteConfirmId.value = profile.id
  deleteConfirmName.value = profile.name
  deleteError.value = null
}

async function doDelete() {
  if (!deleteConfirmId.value) return
  deleting.value = true
  deleteError.value = null
  try {
    await store.deleteProfile(deleteConfirmId.value)
    deleteConfirmId.value = null
  } catch (e: unknown) {
    deleteError.value = e instanceof Error ? e.message : 'Failed to delete profile'
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  if (store.profiles.length === 0) {
    store.fetchProfiles()
  }
})
</script>
