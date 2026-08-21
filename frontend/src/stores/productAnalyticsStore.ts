import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { toDate } from '../lib/formatDate'

interface ConsentData {
  level: 'off' | 'all'
  prompted: 'yes' | 'no' | 'dismissed' | null
  prompted_at: string | null
  level_changed_at: string | null
}

interface ConsentResponse {
  consent: ConsentData
  instance_enabled: boolean
  is_partner_licence: boolean
}

export const useProductAnalyticsStore = defineStore('productAnalytics', () => {
  const consent = ref<ConsentData>({
    level: 'off',
    prompted: null,
    prompted_at: null,
    level_changed_at: null,
  })
  const instanceEnabled = ref(false)
  const isPartnerLicence = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const isOptedIn = computed(() => consent.value.level === 'all')

  const isPromptEligible = computed(() => {
    if (!instanceEnabled.value) return false
    if (consent.value.prompted === 'yes' || consent.value.prompted === 'no') return false
    if (consent.value.prompted === 'dismissed' && consent.value.prompted_at) {
      const dismissedAt = toDate(consent.value.prompted_at)
      if (!dismissedAt) return false
      const cooldownExpiry = new Date(dismissedAt.getTime() + 7 * 24 * 60 * 60 * 1000) // nosemgrep: new-date-without-guard - arithmetic on a validated Date
      return new Date() >= cooldownExpiry
    }
    return consent.value.prompted === null
  })

  const isPartnerCarveOut = computed(() => {
    return isPartnerLicence.value && consent.value.level !== 'all'
  })

  async function fetchConsent(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const resp = await (api as any).GET('/api/v1/admin/product-analytics/consent')
      if (resp.error) {
        error.value = String(resp.error)
        return
      }
      if (resp.data) {
        const data = resp.data as ConsentResponse
        consent.value = data.consent
        instanceEnabled.value = data.instance_enabled
        isPartnerLicence.value = data.is_partner_licence
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function submitConsent(action: 'accept' | 'decline' | 'dismiss'): Promise<boolean> {
    loading.value = true
    error.value = null
    try {
      const resp = await (api as any).POST('/api/v1/admin/product-analytics/consent', {
        body: { action },
      })
      if (resp.error) {
        error.value = String(resp.error)
        return false
      }
      if (resp.data) {
        const data = resp.data as ConsentResponse
        consent.value = data.consent
        instanceEnabled.value = data.instance_enabled
        isPartnerLicence.value = data.is_partner_licence
      }
      return true
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
      return false
    } finally {
      loading.value = false
    }
  }

  async function updateLevel(level: 'off' | 'all'): Promise<boolean> {
    loading.value = true
    error.value = null
    try {
      const resp = await (api as any).PUT('/api/v1/admin/product-analytics/consent', {
        body: { level },
      })
      if (resp.error) {
        error.value = String(resp.error)
        return false
      }
      if (resp.data) {
        const data = resp.data as ConsentResponse
        consent.value = data.consent
        instanceEnabled.value = data.instance_enabled
        isPartnerLicence.value = data.is_partner_licence
      }
      return true
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
      return false
    } finally {
      loading.value = false
    }
  }

  return {
    consent,
    instanceEnabled,
    isPartnerLicence,
    loading,
    error,
    isOptedIn,
    isPromptEligible,
    isPartnerCarveOut,
    fetchConsent,
    submitConsent,
    updateLevel,
  }
})
