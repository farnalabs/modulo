import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'

interface FlagItem {
  name: string
  description: string
  tier: string
  currently_active: boolean
  depends_on: string[] | null
}

interface LicenseInfo {
  tier: string
  has_license_key: boolean
  is_valid: boolean
}

interface FlagsResponse {
  license: LicenseInfo
  flags: FlagItem[]
  would_activate: FlagItem[]
}

export const usePlanStore = defineStore('plan', () => {
  const currentTier = ref('free')
  const features = ref<Record<string, boolean>>({})
  const isLoading = ref(false)
  const error = ref<string | null>(null)

  const isEnterprise = computed(() => currentTier.value === 'enterprise')

  function featureEnabled(name: string): boolean {
    return features.value[name] ?? false
  }

  async function fetchPlan() {
    isLoading.value = true
    error.value = null
    try {
      const { data, error: err } = await (api as any).GET('/api/v1/admin/feature-flags')
      if (err) {
        error.value = String(err)
      } else if (data) {
        const resp = data as FlagsResponse
        currentTier.value = resp.license.tier
        const map: Record<string, boolean> = {}
        for (const flag of resp.flags) {
          map[flag.name] = flag.currently_active
        }
        features.value = map
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      isLoading.value = false
    }
  }

  return { currentTier, features, isLoading, error, isEnterprise, fetchPlan, featureEnabled }
})
