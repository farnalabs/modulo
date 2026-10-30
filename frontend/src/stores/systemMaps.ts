import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { formatApiError } from '../lib/api/formatError'

type SystemMap = components['schemas']['SystemMapResponse']
type SystemMapOverview = components['schemas']['SystemMapOverviewResponse']

export const useSystemMapsStore = defineStore('systemMaps', () => {
  const maps = ref<SystemMap[]>([])
  const overview = ref<SystemMapOverview | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const loaded = ref(false)

  const selectedMap = computed(() => {
    if (overview.value) return maps.value.find((map) => map.id === overview.value?.system_map_id) ?? null
    return maps.value.find((map) => map.default_map && map.published_revision_id) ?? maps.value.find((map) => map.published_revision_id) ?? null
  })
  const hasPublishedMap = computed(() => maps.value.some((map) => map.published_revision_id))

  async function fetchOverview(): Promise<void> {
    loading.value = true
    error.value = null
    overview.value = null
    try {
      const listResponse = await api.GET('/api/v1/system-maps', { params: { query: { page_size: 100 } } })
      if (listResponse.error) throw listResponse.error
      maps.value = listResponse.data?.items ?? []
      const map = maps.value.find((item) => item.default_map && item.published_revision_id) ?? maps.value.find((item) => item.published_revision_id)
      if (!map?.published_revision_id) return

      const overviewResponse = await api.GET('/api/v1/system-maps/{map_id}/overview', { params: { path: { map_id: map.id } } })
      if (overviewResponse.error) throw overviewResponse.error
      overview.value = overviewResponse.data ?? null
    } catch (cause: unknown) {
      error.value = formatApiError(cause)
    } finally {
      loaded.value = true
      loading.value = false
    }
  }

  return { maps, overview, loading, error, loaded, selectedMap, hasPublishedMap, fetchOverview }
})
