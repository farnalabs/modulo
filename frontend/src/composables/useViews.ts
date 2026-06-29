import { ref, computed } from 'vue'
import { api } from '../lib/api/client'

export interface SavedView {
  id: string
  name: string
  view_type: string
  filters: Record<string, any>
  columns?: string[]
  sort_by?: string
  sort_order: string
  created_by: string
  created_at: string
}

export function useViews(viewType: string) {
  const views = ref<SavedView[]>([])
  const currentViewId = ref<string | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const currentView = computed(() =>
    views.value.find(v => v.id === currentViewId.value) ?? null
  )

  async function fetchViews() {
    loading.value = true
    error.value = null
    try {
      const { data, error: apiError } = await api.GET('/api/v1/views', {
        params: { query: { view_type: viewType } },
      })
      if (apiError) throw new Error(apiError.message)
      views.value = (data as any)?.items ?? []
    } catch (e: any) {
      error.value = e.message
    } finally {
      loading.value = false
    }
  }

  function setCurrentView(viewId: string | null) {
    currentViewId.value = viewId
  }

  return {
    views,
    currentViewId,
    currentView,
    loading,
    error,
    fetchViews,
    setCurrentView,
  }
}
