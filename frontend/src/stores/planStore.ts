import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { registerHandler } from './syncRegistry'
import type { EventBusEvent } from '@/types/events'

export const usePlanStore = defineStore('plan', () => {
  const currentTier = ref('community')
  const features = ref<Record<string, boolean>>({})
  const isLoading = ref(false)
  const error = ref<string | null>(null)
  const expiresAt = ref<string | null>(null)
  const orgName = ref<string | null>(null)
  const tierLabels = ref<Record<string, string>>({})
  const tierRanks = ref<Record<string, number>>({})
  const syncingIds = ref(new Set<string>())
  const unsubHandlers: (() => void)[] = []

  const isTeam = computed(() => currentTier.value === 'team')

  function featureEnabled(name: string): boolean {
    return features.value[name] ?? false
  }

  function getTierLabel(tierId: string): string {
    return tierLabels.value[tierId] ?? tierId.charAt(0).toUpperCase() + tierId.slice(1)
  }

  function isAtMinimumTier(minTier: string): boolean {
    const currentRank = tierRanks.value[currentTier.value]
    const minRank = tierRanks.value[minTier]
    if (currentRank === undefined || minRank === undefined) return false
    return currentRank >= minRank
  }

  async function fetchPlan() {
    if (isLoading.value) return
    isLoading.value = true
    error.value = null
    const apiErrors: string[] = []
    try {
      // Feature flags
      try {
        const { data, error: err } = await api.GET('/api/v1/admin/feature-flags')
        if (err) {
          apiErrors.push(`Feature flags: ${formatApiError(err)}`)
        } else if (data) {
          currentTier.value = data.license.tier
          const map: Record<string, boolean> = {}
          for (const flag of data.flags) {
            map[flag.name] = flag.currently_active
          }
          features.value = map
        }
      } catch (e: unknown) {
        apiErrors.push(`Feature flags: ${formatApiError(e)}`)
      }

      error.value = apiErrors.length > 0 ? apiErrors.join('; ') : null

      // License
      try {
        const { data: licenseData, error: licenseError } = await api.GET('/api/v1/admin/license')
        if (licenseError) {
          apiErrors.push(`License: ${formatApiError(licenseError)}`)
        } else if (licenseData) {
          expiresAt.value = licenseData.expires_at ?? null
          orgName.value = licenseData.org_id ?? null
          if (licenseData.tier) currentTier.value = licenseData.tier
        }
      } catch (e: unknown) {
        apiErrors.push(`License: ${formatApiError(e)}`)
      }

      error.value = apiErrors.length > 0 ? apiErrors.join('; ') : null

      // Tiers
      try {
        const { data: tiersData, error: tiersError } = await api.GET('/api/v1/admin/tiers')
        if (tiersError) {
          apiErrors.push(`Tiers: ${formatApiError(tiersError)}`)
        } else if (tiersData) {
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
        apiErrors.push(`Tiers: ${formatApiError(e)}`)
      }

      error.value = apiErrors.length > 0 ? apiErrors.join('; ') : null
    } finally {
      isLoading.value = false
    }
  }

  function handleSyncEvent(event: EventBusEvent): void {
    if (event.type === 'team' || event.type === 'license' || event.type === 'plan') {
      if (!syncingIds.value.has(event.id)) {
        syncingIds.value.add(event.id)
        void fetchPlan().finally(() => {
          syncingIds.value.delete(event.id)
        })
      }
    }
  }

  unsubHandlers.push(registerHandler('team', handleSyncEvent))
  unsubHandlers.push(registerHandler('license', handleSyncEvent))
  unsubHandlers.push(registerHandler('plan', handleSyncEvent))

  if (import.meta.hot) {
    import.meta.hot.dispose(() => { disposeHandlers() })
  }

  function disposeHandlers(): void {
    for (const unsub of unsubHandlers) unsub()
    unsubHandlers.length = 0
    syncingIds.value.clear()
  }

  return { currentTier, features, isLoading, error, isTeam, expiresAt, orgName, tierLabels, tierRanks, fetchPlan, featureEnabled, getTierLabel, isAtMinimumTier, disposeHandlers }
})
