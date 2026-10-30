import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * System Maps is intentionally fail-closed while its discovery/review API is
 * not part of the current generated contract. Keeping this state in a store
 * gives the dashboard and future review route one place to switch on when the
 * API is released, without making speculative requests today.
 */
export const useSystemMapsStore = defineStore('systemMaps', () => {
  const loaded = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const discoveryAvailable = ref(false)
  const draft = ref<null>(null)

  const hasDraft = computed(() => draft.value !== null)

  function reset(): void {
    loaded.value = false
    loading.value = false
    error.value = null
    discoveryAvailable.value = false
    draft.value = null
  }

  /** No request is made until the discovery endpoint exists in schema.ts. */
  async function fetchDiscovery(): Promise<void> {
    loaded.value = true
    discoveryAvailable.value = false
    draft.value = null
  }

  return {
    loaded,
    loading,
    error,
    discoveryAvailable,
    draft,
    hasDraft,
    reset,
    fetchDiscovery,
  }
})
