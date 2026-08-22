import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { toDate } from '../lib/formatDate'
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

const PROMPT_COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000

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
  const consent = ref<ConsentData>(emptyConsent())
  const instanceEnabled = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const isOptedIn = computed(() => consent.value.level === 'all')

  const isPromptEligible = computed(() => {
    if (!instanceEnabled.value) return false
    if (consent.value.prompted === 'yes' || consent.value.prompted === 'no') return false
    if (consent.value.prompted === 'dismissed' && consent.value.prompted_at) {
      const dismissedAt = toDate(consent.value.prompted_at)
      if (!dismissedAt) return false
      const cooldownExpiry = new Date(dismissedAt.getTime() + PROMPT_COOLDOWN_MS) // nosemgrep: new-date-without-guard - arithmetic on a validated Date
      return new Date() >= cooldownExpiry
    }
    return consent.value.prompted === null
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
        error.value = formatApiError('Empty response from product-analytics endpoint')
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

  return {
    consent,
    instanceEnabled,
    loading,
    error,
    isOptedIn,
    isPromptEligible,
    fetchConsent,
    submitConsent,
    updateLevel,
  }
})
