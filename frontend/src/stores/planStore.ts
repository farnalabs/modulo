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
    try {
      let flagsError: string | null = null
      // Feature flags
      try {
        const { data, error: err } = await api.GET('/api/v1/admin/feature-flags')
        if (err) {
          flagsError = formatApiError(err)
        } else if (data) {
          currentTier.value = data.license.tier
          const map: Record<string, boolean> = {}
          for (const flag of data.flags) {
            map[flag.name] = flag.currently_active
          }
          features.value = map
        }
      } catch (e: unknown) {
        flagsError = formatApiError(e)
      }

      error.value = flagsError

      // License
      try {
        const licResp = await api.GET('/api/v1/admin/license')
        if (!licResp.error && licResp.data) {
          expiresAt.value = licResp.data.expires_at ?? null
          orgName.value = licResp.data.org_id ?? null
          if (licResp.data.tier) currentTier.value = licResp.data.tier
        }
      } catch (e: unknown) {
        console.warn('[PlanStore] License fetch failed', e instanceof Error ? e.message : String(e))
      }

      // Tiers
      try {
        const tiersResp = await api.GET('/api/v1/admin/tiers')
        if (!tiersResp.error && tiersResp.data) {
          const labels: Record<string, string> = {}
          const ranks: Record<string, number> = {}
          for (const t of tiersResp.data.tiers) {
            labels[t.tier_id] = t.label
            ranks[t.tier_id] = t.rank
          }
          tierLabels.value = labels
          tierRanks.value = ranks
        }
      } catch (e: unknown) {
        console.warn('[PlanStore] Tiers fetch failed', e instanceof Error ? e.message : String(e))
      }
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
