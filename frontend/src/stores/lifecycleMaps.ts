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

export interface PipelineSummary {
  id: string
  name: string
  description: string | null
}

export interface LifecycleStage {
  id: string
  name: string
  type: 'modulo' | 'external' | 'manual' | 'placeholder'
  x: number
  y: number
  pipeline_id: string | null
  external_url: string | null
  owner: string | null
}

export interface LifecycleEdge {
  id: string
  source: string
  target: string
  trigger_type: string | null
  trigger_description: string | null
  condition: string | null
  estimated_frequency: string | null
}

export const useLifecycleMapsStore = defineStore('lifecycleMaps', () => {
  const { get, post, put, patch } = useApi()

  const maps = ref<LifecycleMapSummary[]>([])
  const currentMap = ref<LifecycleMap | null>(null)
  const currentMapVersion = ref<number | null>(null)
  const isLoading = ref(false)
  const isLoadingDetail = ref(false)
  const error = ref<string | null>(null)
  const detailError = ref<string | null>(null)

  const graduatedCount = computed(() =>
    (currentMap.value?.stages ?? []).filter((s) => s.graduated).length
  )

  const manualCount = computed(() =>
    (currentMap.value?.stages ?? []).filter((s) => s.type === 'manual').length
  )

  const saving = ref(false)
  const pipelines = ref<PipelineSummary[]>([])

  async function fetchMaps(): Promise<void> {
    if (isLoading.value) return
    isLoading.value = true
    error.value = null
    try {
      const data = await get<LifecycleMapSummary[] | { items?: LifecycleMapSummary[] }>('/api/v1/lifecycle-maps')
      if (Array.isArray(data)) {
        maps.value = data
      } else if (data && Array.isArray((data as any).items)) {
        maps.value = (data as any).items as LifecycleMapSummary[]
      } else {
        maps.value = []
      }
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
      if (!data) {
        detailError.value = 'Lifecycle map not found'
        currentMap.value = null
        return
      }
      currentMap.value = data
      currentMapVersion.value = data.current_version ?? null
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
      if (!data) {
        detailError.value = 'Lifecycle map version not found'
        currentMap.value = null
        return
      }
      currentMap.value = data
      currentMapVersion.value = version
    } catch (e: unknown) {
      detailError.value = formatApiError(e)
      currentMap.value = null
    } finally {
      isLoadingDetail.value = false
    }
  }

  async function saveVersion(mapId: string, stages: LifecycleStage[], edges: LifecycleEdge[], notes?: string) {
    saving.value = true
    error.value = null
    try {
      const data = await post<LifecycleMapVersion>(`/api/v1/lifecycle-maps/${mapId}/versions`, {
        stages, edges, notes: notes || ''
      })
      return data
    } catch (e: unknown) {
      error.value = formatApiError(e)
      throw e
    } finally {
      saving.value = false
    }
  }

  async function updateVersion(mapId: string, versionId: string, stages: LifecycleStage[], edges: LifecycleEdge[], notes?: string) {
    saving.value = true
    error.value = null
    try {
      const data = await put<LifecycleMapVersion>(`/api/v1/lifecycle-maps/${mapId}/versions/${versionId}`, {
        stages, edges, notes: notes || ''
      })
      return data
    } catch (e: unknown) {
      error.value = formatApiError(e)
      throw e
    } finally {
      saving.value = false
    }
  }

  async function graduateStage(mapId: string, versionId: string, stageId: string, pipelineId: string) {
    saving.value = true
    error.value = null
    try {
      const data = await patch<LifecycleMapVersion>(
        `/api/v1/lifecycle-maps/${mapId}/versions/${versionId}/stages/${stageId}/graduate`,
        { pipeline_id: pipelineId }
      )
      return data
    } catch (e: unknown) {
      error.value = formatApiError(e)
      throw e
    } finally {
      saving.value = false
    }
  }

  async function fetchPipelines() {
    error.value = null
    try {
      const data = await get<{ items: PipelineSummary[] }>('/api/v1/pipelines?limit=200')
      pipelines.value = data.items || []
    } catch (e: unknown) {
      pipelines.value = []
      error.value = formatApiError(e)
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
    saving,
    pipelines,
    graduatedCount,
    manualCount,
    fetchMaps,
    fetchMap,
    fetchMapVersion,
    saveVersion,
    updateVersion,
    graduateStage,
    fetchPipelines,
  }
})
