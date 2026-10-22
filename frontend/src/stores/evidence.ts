import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/schema'

export type EvidenceIndex = components['schemas']['EvidenceIndex']
export type EvidenceArtifact = components['schemas']['ArtifactEntry']

export const useEvidenceStore = defineStore('evidence', () => {
  const index = ref<EvidenceIndex | null>(null)
  const isLoading = ref(false)
  const error = ref<string | null>(null)

  async function fetchIndex(): Promise<void> {
    if (isLoading.value) return
    isLoading.value = true
    error.value = null
    try {
      const { data, error: responseError, response } = await api.GET('/api/v1/compliance/evidence-index')
      if (responseError || !response?.ok || !data) {
        throw new Error(formatApiError(responseError) || `HTTP ${response?.status ?? 0}`)
      }
      index.value = data
    } catch (err) {
      error.value = formatApiError(err)
    } finally {
      isLoading.value = false
    }
  }

  return { index, isLoading, error, fetchIndex }
})
