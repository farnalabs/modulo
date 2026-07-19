import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import { formatApiError } from '../lib/api/formatError'

export interface EnvironmentProfile {
  id: string
  name: string
  description: string | null
  provider_type: string
  image_ref: string | null
  capabilities: string[]
  network_policy: string
  initialisation_strategy: string
  persistence_policy: string
  status: string
  created_at: string
  updated_at: string
}

export interface EnvironmentProfileSummary {
  id: string
  name: string
  description: string | null
  provider_type: string
  image_ref: string | null
  capabilities: string[]
  status: string
  created_at: string
}

export const useEnvironmentProfilesStore = defineStore('environmentProfiles', () => {
  const { get, post, put, delete: del } = useApi()

  const profiles = ref<EnvironmentProfileSummary[]>([])
  const currentProfile = ref<EnvironmentProfile | null>(null)
  const isLoading = ref(false)
  const isSaving = ref(false)
  const error = ref<string | null>(null)

  async function fetchProfiles(): Promise<void> {
    if (isLoading.value) return
    isLoading.value = true
    error.value = null
    try {
      const data = await get<{ items: EnvironmentProfileSummary[] }>('/api/v1/environment-profiles')
      profiles.value = data.items ?? []
    } catch (e: unknown) {
      error.value = formatApiError(e)
      profiles.value = []
    } finally {
      isLoading.value = false
    }
  }

  async function fetchProfile(id: string): Promise<void> {
    if (isLoading.value) return
    isLoading.value = true
    error.value = null
    try {
      const data = await get<EnvironmentProfile>(`/api/v1/environment-profiles/${id}`)
      currentProfile.value = data
    } catch (e: unknown) {
      error.value = formatApiError(e)
      currentProfile.value = null
    } finally {
      isLoading.value = false
    }
  }

  async function createProfile(data: Partial<EnvironmentProfile>): Promise<void> {
    isSaving.value = true
    error.value = null
    try {
      const created = await post<EnvironmentProfile>('/api/v1/environment-profiles', data)
      profiles.value.push({
        id: created.id,
        name: created.name,
        description: created.description,
        provider_type: created.provider_type,
        image_ref: created.image_ref,
        capabilities: created.capabilities,
        status: created.status,
        created_at: created.created_at,
      })
    } catch (e: unknown) {
      error.value = formatApiError(e)
      throw e
    } finally {
      isSaving.value = false
    }
  }

  async function updateProfile(id: string, data: Partial<EnvironmentProfile>): Promise<void> {
    isSaving.value = true
    error.value = null
    try {
      const updated = await put<EnvironmentProfile>(`/api/v1/environment-profiles/${id}`, data)
      const idx = profiles.value.findIndex((p) => p.id === id)
      if (idx >= 0) {
        profiles.value[idx] = {
          id: updated.id,
          name: updated.name,
          description: updated.description,
          provider_type: updated.provider_type,
          image_ref: updated.image_ref,
          capabilities: updated.capabilities,
          status: updated.status,
          created_at: updated.created_at,
        }
      }
      if (currentProfile.value?.id === id) {
        currentProfile.value = updated
      }
    } catch (e: unknown) {
      error.value = formatApiError(e)
      throw e
    } finally {
      isSaving.value = false
    }
  }

  async function deleteProfile(id: string): Promise<void> {
    isSaving.value = true
    error.value = null
    try {
      await del(`/api/v1/environment-profiles/${id}`)
      profiles.value = profiles.value.filter((p) => p.id !== id)
      if (currentProfile.value?.id === id) {
        currentProfile.value = null
      }
    } catch (e: unknown) {
      error.value = formatApiError(e)
      throw e
    } finally {
      isSaving.value = false
    }
  }

  return {
    profiles,
    currentProfile,
    isLoading,
    isSaving,
    error,
    fetchProfiles,
    fetchProfile,
    createProfile,
    updateProfile,
    deleteProfile,
  }
})
