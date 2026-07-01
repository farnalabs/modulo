import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useApi } from '../composables/useApi'
import type { CompositeDefinition } from '../types/pipeline'

export const useCompositeStore = defineStore('composite', () => {
  const composites = ref<CompositeDefinition[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  const { get } = useApi()

  const compositeMap = computed(() => {
    const map = new Map<string, CompositeDefinition>()
    for (const c of composites.value) {
      map.set(c.id, c)
    }
    return map
  })

  async function loadComposites() {
    if (composites.value.length > 0 && !loading.value) return
    loading.value = true
    error.value = null
    try {
      const result = await get<{ items: CompositeDefinition[] }>('/api/v1/composites')
      composites.value = result.items || []
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
      composites.value = []
    } finally {
      loading.value = false
    }
  }

  function getCompositeById(id: string): CompositeDefinition | undefined {
    return compositeMap.value.get(id)
  }

  function disposeHandlers() {
    composites.value = []
    error.value = null
  }

  return {
    composites,
    loading,
    error,
    loadComposites,
    getCompositeById,
    disposeHandlers,
  }
})
