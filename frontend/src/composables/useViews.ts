import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

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
    if (loading.value) return
    loading.value = true
    error.value = null
    try {
      const resp = await api.GET('/api/v1/views', {
        params: { query: { view_type: viewType } as any },
      })
      if (resp.error) {
        error.value = formatApiError(resp.error)
      } else {
        views.value = resp.data?.items ?? []
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
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
