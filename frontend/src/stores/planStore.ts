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

interface LicenseStatusResponse {
  has_license: boolean
  tier: string
  features: string[]
  expires_at: string | null
  org_id: string | null
}

interface TierInfo {
  tier_id: string
  label: string
  rank: number
}

interface TiersResponse {
  tiers: TierInfo[]
}

export const usePlanStore = defineStore('plan', () => {
  const currentTier = ref('community')
  const features = ref<Record<string, boolean>>({})
  const isLoading = ref(false)
  const error = ref<string | null>(null)
  const expiresAt = ref<string | null>(null)
  const orgName = ref<string | null>(null)
  const tierLabels = ref<Record<string, string>>({})
  const tierRanks = ref<Record<string, number>>({})

  const isTeam = computed(() => currentTier.value === 'team')

  function featureEnabled(name: string): boolean {
    return features.value[name] ?? false
  }

  function getTierLabel(tierId: string): string {
    return tierLabels.value[tierId] ?? tierId.charAt(0).toUpperCase() + tierId.slice(1)
  }

  function isAtMinimumTier(minTier: string): boolean {
    const currentRank = tierRanks.value[currentTier.value] ?? -1
    const minRank = tierRanks.value[minTier] ?? -1
    return currentRank >= minRank
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

      const licResp = await (api as any).GET('/api/v1/admin/license')
      if (!licResp.error && licResp.data) {
        const lic = licResp.data as LicenseStatusResponse
        expiresAt.value = lic.expires_at ?? null
        orgName.value = lic.org_id ?? null
        if (lic.tier) currentTier.value = lic.tier
      }

      const tiersResp = await (api as any).GET('/api/v1/admin/tiers')
      if (!tiersResp.error && tiersResp.data) {
        const tiersData = tiersResp.data as TiersResponse
        const labels: Record<string, string> = {}
        const ranks: Record<string, number> = {}
        for (const t of tiersData.tiers) {
          labels[t.tier_id] = t.label
          ranks[t.tier_id] = t.rank
        }
        tierLabels.value = labels
        tierRanks.value = ranks
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      isLoading.value = false
    }
  }

  return { currentTier, features, isLoading, error, isTeam, expiresAt, orgName, tierLabels, tierRanks, fetchPlan, featureEnabled, getTierLabel, isAtMinimumTier }
})
