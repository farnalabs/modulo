import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useApi } from '../composables/useApi'
import { formatApiError } from '../lib/api/formatError'

export interface LifecycleMapStage {
  id: string
  name: string
  description: string | null
  type: 'modulo' | 'external' | 'manual' | 'placeholder'
  owner_badge: string | null
  graduated: boolean
  pipeline_id: string | null
  external_url: string | null
}

export interface LifecycleMapTransition {
  id: string
  source_stage_id: string
  target_stage_id: string
  trigger_type: string | null
  description: string | null
}

export interface LifecycleMapVersion {
  version: number
  created_at: string
  created_by: string | null
}

export interface LifecycleMap {
  id: string
  name: string
  description: string | null
  owner: string | null
  owner_team_id: string | null
  stages: LifecycleMapStage[]
  transitions: LifecycleMapTransition[]
  versions: LifecycleMapVersion[]
  current_version: number
  created_at: string
  updated_at: string
}

export interface LifecycleMapSummary {
  id: string
  name: string
  description: string | null
  owner: string | null
  owner_team_id: string | null
  stage_count: number
  graduated_count: number
  current_version: number
  created_at: string
  updated_at: string
}

export const useLifecycleMapsStore = defineStore('lifecycleMaps', () => {
  const { get } = useApi()

  const maps = ref<LifecycleMapSummary[]>([])
  const currentMap = ref<LifecycleMap | null>(null)
  const currentMapVersion = ref<number | null>(null)
  const isLoading = ref(false)
  const isLoadingDetail = ref(false)
  const error = ref<string | null>(null)
  const detailError = ref<string | null>(null)

  const graduatedCount = computed(() =>
    currentMap.value?.stages.filter((s) => s.graduated).length ?? 0
  )

  const manualCount = computed(() =>
    currentMap.value?.stages.filter((s) => s.type === 'manual').length ?? 0
  )

  async function fetchMaps(): Promise<void> {
    if (isLoading.value) return
    isLoading.value = true
    error.value = null
    try {
      const data = await get<LifecycleMapSummary[]>('/api/v1/lifecycle-maps')
      maps.value = Array.isArray(data) ? data : []
    } catch (e: unknown) {
      error.value = formatApiError(e)
      maps.value = []
    } finally {
      isLoading.value = false
    }
  }

  async function fetchMap(id: string): Promise<void> {
    isLoadingDetail.value = true
    detailError.value = null
    try {
      const data = await get<LifecycleMap>(`/api/v1/lifecycle-maps/${id}`)
      currentMap.value = data
      currentMapVersion.value = data.current_version
    } catch (e: unknown) {
      detailError.value = formatApiError(e)
      currentMap.value = null
    } finally {
      isLoadingDetail.value = false
    }
  }

  async function fetchMapVersion(id: string, version: number): Promise<void> {
    isLoadingDetail.value = true
    detailError.value = null
    try {
      const data = await get<LifecycleMap>(`/api/v1/lifecycle-maps/${id}/versions/${version}`)
      currentMap.value = data
      currentMapVersion.value = data.current_version
    } catch (e: unknown) {
      detailError.value = formatApiError(e)
    } finally {
      isLoadingDetail.value = false
    }
  }

  return {
    maps,
    currentMap,
    currentMapVersion,
    isLoading,
    isLoadingDetail,
    error,
    detailError,
    graduatedCount,
    manualCount,
    fetchMaps,
    fetchMap,
    fetchMapVersion,
  }
})
