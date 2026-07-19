import { ref, computed, readonly } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

export interface SavedView {
  id: string
  name: string
  view_type: string
  filters: Record<string, any>
  columns?: string[] | null
  sort_by?: string | null
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

  async function fetchViews(force?: boolean) {
    if (!force && loading.value) return
    loading.value = true
    error.value = null
    try {
      const resp = await api.GET('/api/v1/views', {
        params: { query: { view_type: viewType } as unknown as Record<string, unknown> },
      })
      if (resp.error) {
        error.value = formatApiError(resp.error)
      } else {
        views.value = resp.data?.items ?? []
      }
    } catch (e: unknown) {
      error.value = formatApiError(e)
    } finally {
      loading.value = false
    }
  }

  function setCurrentView(viewId: string | null) {
    currentViewId.value = viewId
  }

  return {
    views: readonly(views),
    currentViewId: readonly(currentViewId),
    currentView,
    loading: readonly(loading),
    error: readonly(error),
    fetchViews,
    setCurrentView,
  }
}
