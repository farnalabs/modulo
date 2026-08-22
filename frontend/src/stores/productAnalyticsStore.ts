import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

interface ConsentData {
  level: 'off' | 'all' | string
  prompted: 'yes' | 'no' | 'dismissed' | null
  prompted_at: string | null
  level_changed_at: string | null
  egress_allowed: boolean
  prompt_eligible: boolean
  instance_enabled: boolean
}

interface TransparencyData {
  last_successful_dump_at: string | null
  dump_count_total: number
  consent_level: string
  instance_enabled: boolean
  enforcement_enabled: boolean
  warning: string | null
}

function emptyConsent(): ConsentData {
  return {
    level: 'off',
    prompted: null,
    prompted_at: null,
    level_changed_at: null,
    egress_allowed: false,
    prompt_eligible: false,
    instance_enabled: false,
  }
}

export const useProductAnalyticsStore = defineStore('productAnalytics', () => {
  const { t } = useI18n()
  const consent = ref<ConsentData>(emptyConsent())
  const instanceEnabled = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const transparency = ref<TransparencyData | null>(null)
  const isLoading = ref(false)

  const isOptedIn = computed(() => consent.value.level === 'all')

  // Eligibility is owned by the backend (consent.prompt_eligible) which applies
  // the dismiss-cooldown policy server-side; the client trusts that field rather
  // than re-deriving it (which previously diverged from the backend in a
  // dismissed + null prompted_at corner case).
  const isPromptEligible = computed(() => {
    if (!instanceEnabled.value) return false
    return consent.value.prompt_eligible
  })

  function applyConsent(data: ConsentData) {
    consent.value = {
      level: data.level,
      prompted: data.prompted,
      prompted_at: data.prompted_at ?? null,
      level_changed_at: data.level_changed_at ?? null,
      egress_allowed: data.egress_allowed,
      prompt_eligible: data.prompt_eligible,
      instance_enabled: data.instance_enabled,
    }
    instanceEnabled.value = data.instance_enabled
  }

  async function runRequest(
    request: () => Promise<{ data?: unknown; error?: unknown }>,
  ): Promise<boolean> {
    loading.value = true
    error.value = null
    try {
      const result = await request()
      if (result.error) {
        error.value = formatApiError(result.error)
        return false
      }
      if (!result.data) {
        error.value = formatApiError(t('views.ProductAnalytics.empty_response'))
        return false
      }
      applyConsent(result.data as Parameters<typeof applyConsent>[0])
      return true
    } catch (e: unknown) {
      error.value = formatApiError(e)
      return false
    } finally {
      loading.value = false
    }
  }

  async function fetchConsent(): Promise<boolean> {
    return runRequest(() => api.GET('/api/v1/org/product-analytics'))
  }

  async function submitConsent(action: 'accept' | 'decline' | 'dismiss'): Promise<boolean> {
    return runRequest(() =>
      api.POST('/api/v1/org/product-analytics/consent', { body: { action } }),
    )
  }

  async function updateLevel(level: 'off' | 'all'): Promise<boolean> {
    // The PUT returns only level + level_changed_at, so we re-GET the full
    // consent state and let runRequest apply it. This reuses runRequest's
    // loading/error/finally handling rather than duplicating it here.
    return runRequest(async () => {
      const res = await api.PUT('/api/v1/org/product-analytics', { body: { level } })
      if (res.error) return { error: res.error }
      const getRes = await api.GET('/api/v1/org/product-analytics')
      return { data: getRes.data, error: getRes.error }
    })
  }

  async function fetchTransparency(): Promise<void> {
    isLoading.value = true
    error.value = null
    try {
      const { data, error: apiError } = await api.GET(
        '/api/v1/product-analytics/transparency',
      )
      if (apiError) {
        error.value = formatApiError(apiError)
        return
      }
      transparency.value = data as TransparencyData
    } catch (e: unknown) {
      error.value = formatApiError(e)
    } finally {
      isLoading.value = false
    }
  }

  return {
    consent,
    instanceEnabled,
    loading,
    error,
    isLoading,
    transparency,
    isOptedIn,
    isPromptEligible,
    fetchConsent,
    submitConsent,
    updateLevel,
    fetchTransparency,
  }
})
