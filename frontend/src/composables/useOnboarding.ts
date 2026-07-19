import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '@/lib/api/client'
import { formatApiError } from '@/lib/api/formatError'

export interface OnboardingAction {
  id: string
  title: string
  description: string
  order: number
  icon: string
  route: string | null
  completed: boolean
  skipped: boolean
  auto_check: boolean
}

export const useOnboardingStore = defineStore('onboarding', () => {
  const actions = ref<OnboardingAction[]>([])
  const progressPct = ref(0)
  const isFirstRun = ref(true)
  const dismissed = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const incompleteActions = computed(() => actions.value.filter(a => !a.completed && !a.skipped))
  const completedCount = computed(() => actions.value.filter(a => a.completed).length)
  const totalActions = computed(() => actions.value.length)
  const isActive = computed(() => isFirstRun.value && !dismissed.value)
  const currentAction = computed(() => incompleteActions.value.sort((a, b) => a.order - b.order)[0] ?? null)

  async function fetchStatus() {
    loading.value = true
    error.value = null
    try {
      const { data, error: err } = await api.GET('/api/v1/onboarding/status')
      if (err) {
        error.value = formatApiError(err)
        return
      }
      if (!data) { error.value = 'No response from server'; return }
      actions.value = (data as any).actions ?? []
      const loginAction = actions.value.find(a => a.id === 'login')
      if (loginAction && !loginAction.completed) {
        loginAction.completed = true
      }
      progressPct.value = (data as any).progress_pct ?? 0
      isFirstRun.value = (data as any).is_first_run ?? true
      dismissed.value = (data as any).dismissed ?? false
    } catch (e) {
      error.value = formatApiError(e)
    } finally {
      loading.value = false
    }
  }

  async function completeAction(actionId: string) {
    try {
      const { error: err } = await api.POST('/api/v1/onboarding/actions/{action_id}/complete', {
        params: { path: { action_id: actionId } as any },
      })
      if (err) { error.value = formatApiError(err); return }
      await fetchStatus()
    } catch (e) {
      error.value = formatApiError(e)
    }
  }

  async function skipAction(actionId: string) {
    try {
      const { error: err } = await api.POST('/api/v1/onboarding/actions/{action_id}/skip', {
        params: { path: { action_id: actionId } as any },
      })
      if (err) { error.value = formatApiError(err); return }
      await fetchStatus()
    } catch (e) {
      error.value = formatApiError(e)
    }
  }

  async function dismiss() {
    try {
      const { error: err } = await api.POST('/api/v1/onboarding/dismiss')
      if (err) { error.value = formatApiError(err); return }
      dismissed.value = true
    } catch (e) {
      error.value = formatApiError(e)
    }
  }

  async function seedExamples() {
    try {
      const { data, error: err } = await api.POST('/api/v1/onboarding/seed-examples')
      if (err) { error.value = formatApiError(err); return null }
      await fetchStatus()
      return data ?? null
    } catch (e) {
      error.value = formatApiError(e)
      return null
    }
  }

  return {
    actions, progressPct, isFirstRun, dismissed, loading, error,
    incompleteActions, completedCount, totalActions, isActive, currentAction,
    fetchStatus, completeAction, skipAction, dismiss, seedExamples,
  }
})
